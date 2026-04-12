"""Zmodo sensor platform - motion alert sensors."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util
from datetime import datetime, timezone

from .const import DOMAIN
from .coordinator import ZmodoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zmodo sensors."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for device in coordinator.data["devices"].values():
        # One "last alert" sensor per camera
        entities.append(ZmodoLastAlertSensor(coordinator, device))
        # One "alert count (24h)" sensor per camera
        entities.append(ZmodoAlertCountSensor(coordinator, device))

    async_add_entities(entities, update_before_add=True)


def _alerts_for_device(
    alerts: list[dict], physical_id: str
) -> list[dict]:
    """Filter alerts for a specific device."""
    return [a for a in alerts if a.get("from_id") == physical_id]


class ZmodoLastAlertSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the timestamp of the most recent motion alert."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._device_name: str = device.get("device_name", self._physical_id)
        self._attr_unique_id = f"zmodo_last_alert_{self._physical_id}"
        self._attr_name = "Last Alert"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def native_value(self):  # type: ignore[override]
        """Return the most recent alert timestamp."""
        alerts = _alerts_for_device(
            self.coordinator.data.get("alerts", []), self._physical_id
        )
        if not alerts:
            return None
        # alerts are newest-first per the API
        ts = alerts[0].get("timestamp") or alerts[0].get("alarm_time")
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra attributes from the most recent alert."""
        alerts = _alerts_for_device(
            self.coordinator.data.get("alerts", []), self._physical_id
        )
        if not alerts:
            return {}
        latest = alerts[0]
        return {
            "alert_id": latest.get("id"),
            "image_url": latest.get("image_url"),
            "video_url": latest.get("video_url"),
            "video_duration": latest.get("video_last"),
            "if_read": latest.get("if_read"),
        }


class ZmodoAlertCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the number of motion alerts in the last 24 hours."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "alerts"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._device_name: str = device.get("device_name", self._physical_id)
        self._attr_unique_id = f"zmodo_alert_count_{self._physical_id}"
        self._attr_name = "Alert Count (24h)"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def native_value(self) -> int:
        """Return count of alerts."""
        alerts = _alerts_for_device(
            self.coordinator.data.get("alerts", []), self._physical_id
        )
        return len(alerts)
