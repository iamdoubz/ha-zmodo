"""Zmodo sensor platform — motion alert sensors."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Set up Zmodo sensors — last alert timestamp + 24h count per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoLastAlertSensor(coordinator, device))
        entities.append(ZmodoAlertCountSensor(coordinator, device))

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
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the most recent alert."""
        alert = self._latest_alert
        if not alert:
            return None
        ts = alert.get("timestamp") or alert.get("alarm_time")
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose alert metadata including authenticated media URLs."""
        alert = self._latest_alert
        if not alert:
            return {}

        attrs: dict[str, Any] = {
            "alert_id": alert.get("id"),
            "alert_read": alert.get("if_read") == "1",
            "video_duration_seconds": alert.get("video_last"),
        }

        if alert.get("image_url"):
            attrs["image_url"] = self.coordinator.alert_image_url(alert["image_url"])
        if alert.get("video_url"):
            attrs["video_url"] = self.coordinator.alert_video_url(alert["video_url"])

        return attrs


class ZmodoAlertCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor: number of motion alerts in the last 24 hours for one camera.

    Note: because we now fetch only the *latest* alert per device (count=1),
    this sensor is removed — it cannot be populated without the full list.
    It is kept as a stub that always returns None so existing automations
    referencing the entity don't break; it will show as 'unavailable'.

    To restore full 24h counts, switch coordinator back to bulk alert fetch.
    """

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
    def native_value(self) -> int | None:
        """Not available when using per-device count=1 alert fetching."""
        return None
