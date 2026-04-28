"""Zmodo switch platform — account-level notification toggle."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
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
    """Set up the Zmodo notification switch."""
    coordinator: ZmodoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [ZmodoNotificationSwitch(coordinator, entry)]
    for device in coordinator.data["devices"].values():
        entities.append(ZmodoMicrophoneSwitch(coordinator, device))
    async_add_entities(entities, update_before_add=True)


class ZmodoNotificationSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable or disable Zmodo push notifications for the account.

    This is an account-level toggle (not per-camera), mirroring the
    notifications on/off setting in the Zmodo mobile app.

    State is fetched from /mode/user_mode_get on every coordinator poll
    so it stays in sync if changed from the mobile app.  Toggling calls
    /mode/user_config_set and then immediately requests a coordinator
    refresh so the UI reflects the new state without waiting for the
    next scheduled poll.
    """

    _attr_has_entity_name = True
    _attr_name = "Notifications"
    _attr_icon = "mdi:bell"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        # Unique to the account (config entry), not per-device
        self._attr_unique_id = f"zmodo_notifications_{entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Associate with the first device found, or create a virtual account device."""
        devices = self.coordinator.data.get("devices", {})
        if devices:
            # Attach to the first physical device so the switch appears
            # somewhere sensible in the device list
            first_id = next(iter(devices))
            dev = devices[first_id]
            return DeviceInfo(
                identifiers={(DOMAIN, first_id)},
                name=dev.get("device_name", first_id),
                manufacturer="Zmodo",
                model=dev.get("device_model"),
            )
        # Fallback: a virtual "account" device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name="Zmodo Account",
            manufacturer="Zmodo",
        )

    @property
    def is_on(self) -> bool:
        """Return True when notifications are enabled."""
        return self.coordinator.data.get("notifications_on", True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable push notifications."""
        await self.coordinator.async_set_notifications(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable push notifications."""
        await self.coordinator.async_set_notifications(False)


class ZmodoMicrophoneSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable or disable the microphone on a Zmodo camera.

    The device list returns a "mute" field for each device:
      mute = "1" → microphone is active (switch ON)
      mute = "0" → microphone is muted  (switch OFF)

    The API uses the same inverted convention; the inversion is handled
    in coordinator.async_set_device_mute() and api.set_device_mute() so
    this entity works with plain on/off semantics.
    """

    _attr_has_entity_name = True
    _attr_name = "Microphone"
    _attr_icon = "mdi:microphone"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ZmodoCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._physical_id: str = device["physical_id"]
        self._attr_unique_id = f"zmodo_microphone_{self._physical_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._physical_id)})

    @property
    def _current_device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._physical_id, {})

    @property
    def is_on(self) -> bool:
        """Return True when the microphone is active (mute field = "1")."""
        return self._current_device.get("mute", "0") == "1"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._current_device.get("device_online", "0") == "1"
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate the microphone."""
        await self.coordinator.async_set_device_mute(self._physical_id, mic_active=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mute the microphone."""
        await self.coordinator.async_set_device_mute(self._physical_id, mic_active=False)
