"""Zmodo camera platform.

* ``ZmodoCamera`` — live view via the local FLV relay (see relay.py).
* ``ZmodoAlertCamera`` — replays the latest motion alert's MP4 clip.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SNAPSHOT_CACHE_SECONDS
from .coordinator import ZmodoCoordinator
from .relay import (
    alert_media_loopback_url,
    alert_media_relay_path,
    find_live_hub,
    relay_url,
)

_LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a live camera and an alert-clip camera for every device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[Camera] = []
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoCamera(coordinator, entry, device))
        entities.append(ZmodoAlertCamera(coordinator, entry, device))
    async_add_entities(entities)


def _device_info(device: dict[str, Any]) -> DeviceInfo:
    serial = device["physical_id"]
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=device.get("device_name", serial),
        manufacturer="Zmodo",
        model=device.get("device_model") or device.get("product_id"),
        sw_version=device.get("device_version"),
        serial_number=serial,
    )


class ZmodoCamera(CoordinatorEntity, Camera):
    """Live view for one Zmodo camera (SD stream from user.zmodo.com)."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        entry: ConfigEntry,
        device: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._entry_id = entry.entry_id
        self._initial_device = device
        self._physical_id: str = device["physical_id"]
        # Keeps the unique_id of the old SD entity so history carries over.
        self._attr_unique_id = f"zmodo_camera_{self._physical_id}_sd"
        self._snapshot: bytes | None = None
        self._snapshot_at = 0.0
        self._snapshot_lock = asyncio.Lock()

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(
            self._physical_id, self._initial_device
        )

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._current_device)

    @property
    def name(self) -> str:
        # has_entity_name: HA prefixes the device name ("Living Room SD")
        return "SD"

    @property
    def available(self) -> bool:
        return super().available and self._current_device.get("device_online") == "1"

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dev = self._current_device
        return {
            "serial": self._physical_id,
            "platform_id": dev.get("platform_id"),
            "product_id": dev.get("product_id"),
            "quality": "SD",
            "viewers": 0,
            "zmodo_delay_seconds": None,
        } | self._hub_attributes()

    def _hub_attributes(self) -> dict[str, Any]:
        hub = find_live_hub(self.hass, self._entry_id, self._physical_id)
        if hub is None:
            return {}
        return {
            "viewers": hub.client_count,
            # Camera on-screen clock vs HA clock as frames arrive from Zmodo:
            # the part of the delay that happens before Home Assistant.
            "zmodo_delay_seconds": (
                round(hub.camera_delay, 1) if hub.camera_delay is not None and hub.client_count else None
            ),
        }

    async def stream_source(self) -> str | None:
        return relay_url(self.hass, self._entry_id, self._physical_id)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Grab a still from the live stream (cached; each grab opens a session)."""
        async with self._snapshot_lock:
            if self._snapshot and time.monotonic() - self._snapshot_at < SNAPSHOT_CACHE_SECONDS:
                return self._snapshot
            image = await async_get_image(
                self.hass,
                relay_url(self.hass, self._entry_id, self._physical_id),
                width=width,
                height=height,
            )
            if image:
                self._snapshot = image
                self._snapshot_at = time.monotonic()
            return image or self._snapshot


class ZmodoAlertCamera(CoordinatorEntity, Camera):
    """Camera entity that replays the latest motion alert's MP4 clip.

    stream_source() is the loopback alert-media relay (the platform API needs
    a Bearer header ffmpeg can't send); the still is the alert's image.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Alert Clip"
    _attr_icon = "mdi:motion-sensor"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        entry: ConfigEntry,
        device: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._entry_id = entry.entry_id
        self._initial_device = device
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_clip_{self._physical_id}"
        self._image_cache: tuple[str, bytes] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(
            self.coordinator.data["devices"].get(self._physical_id, self._initial_device)
        )

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def available(self) -> bool:
        return super().available and self._latest_alert is not None

    async def stream_source(self) -> str | None:
        alert = self._latest_alert
        if not alert:
            return None
        return alert_media_loopback_url(self.hass, self._entry_id, alert["id"], "clip")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        alert = self._latest_alert
        if not alert or not alert.get("has_image"):
            return None
        if self._image_cache and self._image_cache[0] == alert["id"]:
            return self._image_cache[1]
        try:
            image, _ctype = await self.coordinator.platform.get_alert_image(alert["id"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Alert image for %s failed: %s", self._physical_id, err)
            return None
        self._image_cache = (alert["id"], image)
        return image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        return {
            "alert_id": alert.get("id"),
            "reason": alert.get("reason"),
            "alert_time": alert.get("ts"),
            "alert_read": bool(alert.get("read")),
            "video_url": alert_media_relay_path(self._entry_id, alert["id"], "clip"),
            "image_url": alert_media_relay_path(self._entry_id, alert["id"], "image")
            if alert.get("has_image") else None,
        }
