"""Zmodo image platform — latest motion alert thumbnail per camera."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ZmodoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one thumbnail image entity per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        ZmodoAlertImage(coordinator, device)
        for device in coordinator.data["devices"].values()
    ]
    async_add_entities(entities, update_before_add=True)


class ZmodoAlertImage(CoordinatorEntity, ImageEntity):
    """Image entity that serves the thumbnail JPEG of the latest motion alert.

    HA proxies the image through its own API (/api/image_proxy/IMAGE_ENTITY_ID)
    so the Zmodo token never needs to be embedded in dashboard URLs. The token
    is resolved fresh from coordinator.alert_image_url() every time HA fetches
    the image bytes, so post-refresh tokens are used automatically.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Alert Image"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_image_{self._physical_id}"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    # ------------------------------------------------------------------
    # Latest alert helpers
    # ------------------------------------------------------------------

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    # ------------------------------------------------------------------
    # ImageEntity contract
    # ------------------------------------------------------------------

    @property
    def image_last_updated(self) -> datetime | None:
        """Return when the current image was captured.

        HA uses this to decide whether to re-fetch the image bytes.
        Changing this value triggers a re-fetch.
        """
        alert = self._latest_alert
        if not alert:
            return None
        ts = alert.get("timestamp") or alert.get("alarm_time")
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)

    async def async_image(self) -> bytes | None:
        """Fetch and return the raw JPEG bytes of the latest alert thumbnail.

        Called by HA whenever image_last_updated changes or a client requests
        /api/image_proxy/<entity_id>.  The token is resolved here so it is
        always current.
        """
        alert = self._latest_alert
        if not alert or not alert.get("image_url"):
            return None

        url = self.coordinator.alert_image_url(alert["image_url"])
        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.debug(
                    "Alert image fetch for %s returned HTTP %d",
                    self._physical_id,
                    resp.status,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Alert image fetch for %s failed: %s", self._physical_id, err
            )

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the video URL and metadata as attributes."""
        alert = self._latest_alert
        if not alert:
            return {}

        attrs: dict[str, Any] = {
            "alert_id": alert.get("id"),
            "video_duration_seconds": alert.get("video_last"),
            "alert_read": alert.get("if_read") == "1",
        }

        # Add the authenticated video URL so automations / Lovelace can use it
        if alert.get("video_url"):
            attrs["video_url"] = self.coordinator.alert_video_url(alert["video_url"], self._physical_id)

        # Also include the raw (unauthenticated) paths for reference
        attrs["image_path"] = alert.get("image_url")
        attrs["video_path"] = alert.get("video_url")

        return attrs
