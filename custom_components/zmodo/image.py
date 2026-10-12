"""Zmodo image platform — latest motion alert thumbnail per camera."""
from __future__ import annotations

import logging
from datetime import datetime, UTC
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
from .relay import alert_media_relay_path

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one thumbnail image entity per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoAlertImage(coordinator, device))
        entities.append(ZmodoDeviceImage(coordinator, device))
    async_add_entities(entities, update_before_add=True)


class ZmodoAlertImage(CoordinatorEntity, ImageEntity):
    """Still image of the latest motion alert (platform API).

    HA serves it through /api/image_proxy/<entity_id>, so the Zmodo bearer
    token never reaches the browser.
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

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _latest_alert(self) -> dict | None:
        alert = self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)
        return alert if alert and alert.get("has_image") else None

    @property
    def image_last_updated(self) -> datetime | None:
        """Changes whenever a newer alert arrives, which triggers a re-fetch."""
        if not self._latest_alert:
            return None
        return self.coordinator.data.get("last_alert_time", {}).get(self._physical_id)

    async def async_image(self) -> bytes | None:
        alert = self._latest_alert
        if not alert:
            return None
        try:
            image, content_type = await self.coordinator.platform.get_alert_image(alert["id"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Alert image fetch for %s failed: %s", self._physical_id, err)
            return None
        self._attr_content_type = content_type
        return image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        entry_id = self.coordinator.entry_id
        return {
            "alert_id": alert.get("id"),
            "reason": alert.get("reason"),
            "alert_read": bool(alert.get("read")),
            "image_url": alert_media_relay_path(entry_id, alert["id"], "image"),
            "video_url": alert_media_relay_path(entry_id, alert["id"], "clip"),
        }


class ZmodoDeviceImage(CoordinatorEntity, ImageEntity):
    """Image entity showing the physical device product photo.

    The image is fetched from the storage_list pic_url via the alarm server
    proxy endpoint, which requires the token and physical_id.  Because the
    device photo never changes between polls, image_last_updated is set once
    at entity creation so HA fetches it exactly once and caches it.
    """

    _attr_has_entity_name = True
    _attr_name = "Device Image"
    _attr_content_type = "image/png"
    _attr_entity_category = None  # show in the main entity list

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_device_image_{self._physical_id}"
        # Fixed timestamp — device image doesn't change, so HA fetches it once
        self._created_at: datetime = datetime.now(UTC)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _pic_url(self) -> str | None:
        """Return the raw pic_url path for this device, or None if unavailable."""
        return self.coordinator.data.get("device_pics", {}).get(self._physical_id)

    @property
    def image_last_updated(self) -> datetime | None:
        """Return a fixed timestamp so HA fetches the image once and caches it."""
        return self._created_at if self._pic_url else None

    async def async_image(self) -> bytes | None:
        """Fetch and return the device product image bytes."""
        pic_path = self._pic_url
        if not pic_path:
            return None

        url = self.coordinator.device_pic_url(pic_path, self._physical_id)
        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.debug(
                    "Device image fetch for %s returned HTTP %d",
                    self._physical_id,
                    resp.status,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Device image fetch for %s failed: %s", self._physical_id, err
            )

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pic_path = self._pic_url
        if not pic_path:
            return {}
        return {
            "pic_path": pic_path,
            "image_url": self.coordinator.device_pic_url(pic_path, self._physical_id),
        }
