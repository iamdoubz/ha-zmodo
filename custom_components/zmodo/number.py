"""Zmodo number platform — per-device speaker volume control."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    """Set up one volume number entity per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ZmodoVolumeNumber(coordinator, device)
            for device in coordinator.data["devices"].values()
        ],
        update_before_add=True,
    )


class ZmodoVolumeNumber(CoordinatorEntity, NumberEntity):
    """Number entity to get and set the speaker volume of a Zmodo camera.

    The current volume is read directly from the device data already fetched
    by the coordinator (device_list returns device_volume for every device),
    so no extra API call is needed to display the current value.

    Setting the value POSTs to /device/device_modify via the coordinator,
    which also applies an optimistic update so the slider responds immediately.
    """

    _attr_has_entity_name = True
    _attr_name = "Volume"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "%"

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_volume_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._physical_id, {})

    @property
    def native_value(self) -> float:
        """Return the current volume from coordinator device data."""
        return float(self._current_device.get("device_volume", 50))

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._current_device.get("device_online", "0") == "1"
        )

    async def async_set_native_value(self, value: float) -> None:
        """Called by HA when the user moves the slider."""
        await self.coordinator.async_set_device_volume(
            self._physical_id, int(value)
        )
