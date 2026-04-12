"""Zmodo camera platform — one SD and one HD entity per physical device."""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    STREAM_BASE_URL,
    STREAM_MEDIA_TYPE_HD,
    STREAM_MEDIA_TYPE_SD,
)
from .coordinator import ZmodoCoordinator

_LOGGER = logging.getLogger(__name__)

_QUALITY_LABEL = {
    STREAM_MEDIA_TYPE_SD: "SD",
    STREAM_MEDIA_TYPE_HD: "HD",
}


def _build_stream_url(
    physical_id: str,
    token: str,
    aes_key: str,
    device_type: str,
    channel: int = 0,
    media_type: int = STREAM_MEDIA_TYPE_HD,
    cid: int = 0,
) -> str:
    """Build the flv.meshare.com live stream URL for a device."""
    params = {
        "devid": physical_id,
        "token": token,
        "media_type": media_type,
        "channel": channel,
        "start_time": int(time.time()),
        "cid": cid,
        "aes_key": aes_key,
        "has_audio": 1,
        "device_type": device_type,
    }
    return f"{STREAM_BASE_URL}?{urlencode(params)}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zmodo cameras — SD and HD entity for every device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[Camera] = []
    for device in coordinator.data["devices"].values():
        for media_type in (STREAM_MEDIA_TYPE_SD, STREAM_MEDIA_TYPE_HD):
            entities.append(ZmodoCamera(coordinator, device, media_type))
        entities.append(ZmodoAlertCamera(coordinator, device))

    async_add_entities(entities, update_before_add=True)


class ZmodoCamera(CoordinatorEntity, Camera):
    """One stream quality (SD or HD) for a Zmodo camera device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
        media_type: int,
    ) -> None:
        """Initialise the camera entity.

        The token is NOT stored at construction time — it is always read from
        coordinator.token so that proactively refreshed tokens are used
        immediately without requiring an entity reload.
        """
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._initial_device = device
        self._physical_id: str = device["physical_id"]
        self._media_type = media_type
        self._quality_label = _QUALITY_LABEL[media_type]

        self._attr_unique_id = (
            f"zmodo_camera_{self._physical_id}_{self._quality_label.lower()}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(
            self._physical_id, self._initial_device
        )

    def _stream_url(self) -> str:
        """Build a fresh stream URL using the coordinator's current token."""
        dev = self._current_device
        channel = int(dev.get("device_channel", 1)) - 1
        return _build_stream_url(
            physical_id=self._physical_id,
            token=self.coordinator.token,   # always fresh — never cached
            aes_key=dev.get("aes_key", ""),
            device_type=dev.get("device_type", "22"),
            channel=channel,
            media_type=self._media_type,
            cid=0,
        )

    # ------------------------------------------------------------------
    # Entity / device identity
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._current_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._physical_id)},
            name=dev.get("device_name", self._physical_id),
            manufacturer="Zmodo",
            model=dev.get("device_model"),
            sw_version=dev.get("device_version"),
        )

    @property
    def name(self) -> str:
        device_name = self._current_device.get("device_name", self._physical_id)
        return f"{device_name} ({self._quality_label})"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._current_device.get("device_on", "1") == "1"

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._current_device.get("device_online", "0") == "1"
        )

    @property
    def supported_features(self) -> CameraEntityFeature:
        return CameraEntityFeature.STREAM

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dev = self._current_device
        return {
            "physical_id": self._physical_id,
            "quality": self._quality_label,
            "media_type": self._media_type,
            "device_model": dev.get("device_model"),
            "device_mac": dev.get("device_mac"),
            "device_online": dev.get("device_online"),
            "device_version": dev.get("device_version"),
            "time_zone": dev.get("time_zone"),
            "resolution": dev.get("resolution"),
            "nightvision": dev.get("nightvision"),
            "motion_sensitivity": dev.get("motion_sensitivity"),
            "sound_detection": dev.get("sound_detection"),
        }

    # ------------------------------------------------------------------
    # Stream / image
    # ------------------------------------------------------------------

    async def stream_source(self) -> str | None:
        """Return the FLV stream URL with the current fresh token."""
        return self._stream_url()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Fetch a still by reading the first chunk of this stream."""
        url = self._stream_url()
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return await resp.content.read(65536)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Could not fetch still image for %s (%s): %s",
                self._physical_id,
                self._quality_label,
                err,
            )
        return None


class ZmodoAlertCamera(CoordinatorEntity, Camera):
    """Camera entity that replays the latest motion alert video clip.

    stream_source() points to the MP4 alert clip (BT.709 / AAC) so it
    can be played directly in the HA frontend via the stream component.
    async_camera_image() serves the alert thumbnail JPEG as the static
    preview, giving the dashboard card a meaningful still frame.

    Both URLs are re-resolved from the coordinator on every call so they
    always carry the current token after a proactive refresh.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Alert Clip"
    _attr_icon = "mdi:motion-sensor"

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._initial_device = device
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_clip_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        dev = self.coordinator.data["devices"].get(
            self._physical_id, self._initial_device
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._physical_id)},
            name=dev.get("device_name", self._physical_id),
            manufacturer="Zmodo",
            model=dev.get("device_model"),
            sw_version=dev.get("device_version"),
        )

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def supported_features(self) -> CameraEntityFeature:
        return CameraEntityFeature.STREAM

    @property
    def available(self) -> bool:
        """Available when the device is online and has at least one alert."""
        dev = self.coordinator.data["devices"].get(self._physical_id, {})
        return (
            super().available
            and dev.get("device_online", "0") == "1"
            and self._latest_alert is not None
        )

    async def stream_source(self) -> str | None:
        """Return the authenticated MP4 URL of the latest alert clip."""
        alert = self._latest_alert
        if not alert or not alert.get("video_url"):
            return None
        return self.coordinator.alert_video_url(alert["video_url"])

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the alert thumbnail JPEG as the static preview frame."""
        alert = self._latest_alert
        if not alert or not alert.get("image_url"):
            return None

        url = self.coordinator.alert_image_url(alert["image_url"])
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Alert clip thumbnail fetch for %s failed: %s",
                self._physical_id, err,
            )
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        attrs: dict[str, Any] = {
            "alert_id": alert.get("id"),
            "video_duration_seconds": alert.get("video_last"),
            "alert_read": alert.get("if_read") == "1",
        }
        if alert.get("video_url"):
            attrs["video_url"] = self.coordinator.alert_video_url(alert["video_url"])
        if alert.get("image_url"):
            attrs["image_url"] = self.coordinator.alert_image_url(alert["image_url"])
        return attrs
