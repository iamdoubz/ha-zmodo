"""Zmodo select platform — per-device frame rate selector."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ZmodoCoordinator

_LOGGER = logging.getLogger(__name__)

FRAME_RATE_OPTIONS = ["10", "20", "25"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one frame rate select entity per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ZmodoFrameRateSelect(coordinator, device)
            for device in coordinator.data["devices"].values()
        ],
        update_before_add=True,
    )


class ZmodoFrameRateSelect(CoordinatorEntity, SelectEntity):
    """Select entity to get and set the frame rate of a Zmodo camera.

    The three available options (10, 20, 25 fps) are fixed by the device
    firmware.  The current value is read from the device data already in
    the coordinator, so no extra API call is needed to display it.
    """

    _attr_has_entity_name = True
    _attr_name = "Frame Rate"
    _attr_icon = "mdi:filmstrip"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = FRAME_RATE_OPTIONS

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_frame_rate_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._physical_id, {})

    @property
    def current_option(self) -> str:
        """Return the current frame rate as a string, defaulting to '10'."""
        value = str(self._current_device.get("frame_rate", "10"))
        # Guard against an unexpected value from the API
        return value if value in FRAME_RATE_OPTIONS else "10"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._current_device.get("device_online", "0") == "1"
        )

    async def async_select_option(self, option: str) -> None:
        """Called by HA when the user picks a new frame rate."""
        await self.coordinator.async_set_device_frame_rate(
            self._physical_id, int(option)
        )
