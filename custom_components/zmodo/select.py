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

# Night vision mode: API value -> human-readable label
NIGHTVISION_OPTIONS = ["Auto", "Always On", "Always Off"]
NIGHTVISION_API_MAP = {"Auto": "1", "Always On": "2", "Always Off": "3"}
NIGHTVISION_LABEL_MAP = {"1": "Auto", "2": "Always On", "3": "Always Off"}

# Night vision sensitivity: API value -> human-readable label
NIGHT_LEVEL_OPTIONS = ["Low", "Normal", "High"]
NIGHT_LEVEL_API_MAP = {"Low": "0", "Normal": "1", "High": "2"}
NIGHT_LEVEL_LABEL_MAP = {"0": "Low", "1": "Normal", "2": "High"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one frame rate select entity per device."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoFrameRateSelect(coordinator, device))
        entities.append(ZmodoNightvisionSelect(coordinator, device))
        entities.append(ZmodoNightLevelSelect(coordinator, device))
    async_add_entities(entities, update_before_add=True)


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


class ZmodoNightvisionSelect(CoordinatorEntity, SelectEntity):
    """Select entity to control the night vision mode of a Zmodo camera.

    Options:
      Auto       (API value 1) — camera decides automatically
      Always On  (API value 2) — IR LEDs always active
      Always Off (API value 3) — IR LEDs always off
    """

    _attr_has_entity_name = True
    _attr_name = "Night Vision Mode"
    _attr_icon = "mdi:weather-night"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = NIGHTVISION_OPTIONS

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_nightvision_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._physical_id, {})

    @property
    def current_option(self) -> str:
        """Return the current night vision mode as a human-readable label."""
        value = str(self._current_device.get("nightvision", "1"))
        return NIGHTVISION_LABEL_MAP.get(value, "Auto")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._current_device.get("device_online", "0") == "1"
        )

    async def async_select_option(self, option: str) -> None:
        """Called by HA when the user picks a new night vision mode."""
        api_value = int(NIGHTVISION_API_MAP[option])
        await self.coordinator.async_set_device_nightvision(self._physical_id, api_value)


class ZmodoNightLevelSelect(CoordinatorEntity, SelectEntity):
    """Select entity to control the night vision sensitivity of a Zmodo camera.

    Only active when the night vision mode is set to Auto (nightvision=1).

    Options:
      Low    (API value 0)
      Normal (API value 1)
      High   (API value 2)
    """

    _attr_has_entity_name = True
    _attr_name = "Night Vision Sensitivity"
    _attr_icon = "mdi:brightness-6"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = NIGHT_LEVEL_OPTIONS

    def __init__(self, coordinator: ZmodoCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_night_level_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._physical_id, {})

    @property
    def current_option(self) -> str:
        """Return the current sensitivity level as a human-readable label."""
        value = str(self._current_device.get("night_level", "1"))
        return NIGHT_LEVEL_LABEL_MAP.get(value, "Normal")

    @property
    def available(self) -> bool:
        """Only available when the camera is online and nightvision is Auto."""
        dev = self._current_device
        return (
            super().available
            and dev.get("device_online", "0") == "1"
            and str(dev.get("nightvision", "1")) == "1"
        )

    async def async_select_option(self, option: str) -> None:
        """Called by HA when the user picks a new sensitivity level."""
        api_value = int(NIGHT_LEVEL_API_MAP[option])
        await self.coordinator.async_set_device_night_level(self._physical_id, api_value)
