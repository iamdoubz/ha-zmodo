"""Zmodo sensor platform — motion alert sensors."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Set up Zmodo sensors — last alert timestamp + 24h count per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoLastAlertSensor(coordinator, device))
        entities.append(ZmodoAlertCountSensor(coordinator, device))
        entities.append(ZmodoAlertImageUrlSensor(coordinator, device))
        entities.append(ZmodoAlertVideoUrlSensor(coordinator, device))

    async_add_entities(entities, update_before_add=True)


class ZmodoLastAlertSensor(CoordinatorEntity, SensorEntity):
    """Sensor: timestamp of the most recent motion alert for one camera."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Last Alert"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_last_alert_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _latest_alert(self) -> dict | None:
        """Newest alert from the platform API."""
        alerts = self.coordinator.data.get("platform_alerts", {}).get(self._physical_id)
        return alerts[0] if alerts else None

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the most recent alert."""
        return self.coordinator.data.get("last_alert_time", {}).get(self._physical_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose alert metadata, plus legacy media URLs when available."""
        alert = self._latest_alert
        if not alert:
            return {}

        attrs: dict[str, Any] = {
            "alert_id": alert.get("id"),
            "reason": alert.get("reason"),
            "severity": alert.get("severity"),
            "alert_read": bool(alert.get("read")),
            "has_image": bool(alert.get("has_image")),
        }

        entry_id = self.coordinator.entry_id
        if alert.get("has_image"):
            attrs["image_url"] = alert_media_relay_path(entry_id, alert["id"], "image")
        attrs["video_url"] = alert_media_relay_path(entry_id, alert["id"], "clip")

        return attrs


class ZmodoAlertCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor: number of motion alerts in the last 24 hours for one camera."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "alerts"
    _attr_icon = "mdi:bell-ring"
    _attr_name = "Alert Count (24h)"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_count_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def native_value(self) -> int:
        """Return the number of motion alerts in the last 24 hours."""
        return self.coordinator.data.get("alert_counts", {}).get(self._physical_id, 0)


class _AlertUrlSensor(CoordinatorEntity, SensorEntity):
    """Base: relative, HA-authenticated relay path to the latest alert's media.

    Usable directly in companion-app notifications (``image:``/``video:``)
    and by any client sending an HA access token. Paths stay stable per alert.
    """

    _attr_has_entity_name = True
    _kind = "image"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def native_value(self) -> str | None:
        alert = self._latest_alert
        if not alert or (self._kind == "image" and not alert.get("has_image")):
            return None
        return alert_media_relay_path(self.coordinator.entry_id, alert["id"], self._kind)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        return {"alert_id": alert.get("id"), "alert_time": alert.get("ts")}


class ZmodoAlertImageUrlSensor(_AlertUrlSensor):
    """Sensor: relay path of the latest alert still image."""

    _attr_name = "Last Alert Image URL"
    _attr_icon = "mdi:image"
    _kind = "image"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"zmodo_alert_image_url_{self._physical_id}"


class ZmodoAlertVideoUrlSensor(_AlertUrlSensor):
    """Sensor: relay path of the latest alert MP4 clip."""

    _attr_name = "Last Alert Video URL"
    _attr_icon = "mdi:video"
    _kind = "clip"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"zmodo_alert_video_url_{self._physical_id}"
