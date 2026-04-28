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
            attrs["video_url"] = self.coordinator.alert_video_url(alert["video_url"], self._physical_id)

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


class ZmodoAlertImageUrlSensor(CoordinatorEntity, SensorEntity):
    """Sensor: authenticated URL of the latest alert thumbnail image.

    The state is a plain URL string so it can be used directly in
    Lovelace picture cards, template sensors, and automations without
    needing the image_proxy endpoint.  The URL is re-resolved from the
    coordinator on every state update so it always carries a fresh token.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Alert Image URL"
    _attr_icon = "mdi:image"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_image_url_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def native_value(self) -> str | None:
        """Return the full authenticated image URL, or None if no alert."""
        alert = self._latest_alert
        if not alert or not alert.get("image_url"):
            return None
        return self.coordinator.alert_image_url(alert["image_url"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        return {
            "alert_id": alert.get("id"),
            "image_path": alert.get("image_url"),
        }


class ZmodoAlertVideoUrlSensor(CoordinatorEntity, SensorEntity):
    """Sensor: authenticated URL of the latest alert video clip.

    The state is a plain URL string that can be passed to a media player
    card or used in automations.  The video is MP4 / BT.709 with AAC audio.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Alert Video URL"
    _attr_icon = "mdi:video"

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_alert_video_url_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _latest_alert(self) -> dict | None:
        return self.coordinator.data.get("latest_alerts", {}).get(self._physical_id)

    @property
    def native_value(self) -> str | None:
        """Return the full authenticated video URL, or None if no alert."""
        alert = self._latest_alert
        if not alert or not alert.get("video_url"):
            return None
        return self.coordinator.alert_video_url(alert["video_url"], self._physical_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._latest_alert
        if not alert:
            return {}
        return {
            "alert_id": alert.get("id"),
            "video_duration_seconds": alert.get("video_last"),
            "video_path": alert.get("video_url"),
        }
