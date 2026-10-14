"""Zmodo button platform — cloud PTZ pan/tilt controls.

One button per direction for each PTZ-capable camera. A press sends a timed
nudge (pan for ~0.6 s, then stop) over the cloud command channel — see
:mod:`.ptz`.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ZmodoCoordinator
from .ptz import supports_ptz

_LOGGER = logging.getLogger(__name__)

# direction -> (label, mdi icon)
PTZ_BUTTONS: dict[str, tuple[str, str]] = {
    "up": ("Pan up", "mdi:arrow-up-bold"),
    "down": ("Pan down", "mdi:arrow-down-bold"),
    "left": ("Pan left", "mdi:arrow-left-bold"),
    "right": ("Pan right", "mdi:arrow-right-bold"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add PTZ buttons for each pan/tilt-capable camera."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ZmodoPtzButton(coordinator, device, direction)
        for device in coordinator.data["devices"].values()
        if supports_ptz(device)
        for direction in PTZ_BUTTONS
    ]
    async_add_entities(entities)


class ZmodoPtzButton(CoordinatorEntity, ButtonEntity):
    """A single PTZ direction button for one camera."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
        direction: str,
    ) -> None:
        super().__init__(coordinator)
        self._serial: str = device["physical_id"]
        self._direction = direction
        label, icon = PTZ_BUTTONS[direction]
        self._attr_name = label
        self._attr_icon = icon
        self._attr_unique_id = f"zmodo_ptz_{direction}_{self._serial}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def available(self) -> bool:
        dev = self.coordinator.data["devices"].get(self._serial, {})
        return super().available and dev.get("device_online") == "1"

    async def async_press(self) -> None:
        try:
            await self.coordinator.ptz.nudge(self._serial, self._direction)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Zmodo PTZ {self._direction} failed: {err}"
            ) from err
