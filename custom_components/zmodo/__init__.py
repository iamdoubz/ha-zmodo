"""Zmodo integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ZmodoApi
from .const import DOMAIN
from .coordinator import ZmodoCoordinator
from .platform_api import ZmodoPlatformApi
from .relay import async_register_relay, async_stop_live_hubs

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA, Platform.IMAGE, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Zmodo from a config entry."""
    session = async_get_clientsession(hass)
    api = ZmodoApi(session)
    platform = ZmodoPlatformApi(
        session, entry.data.get("email", ""), entry.data.get("password", "")
    )

    coordinator = ZmodoCoordinator(
        hass=hass, api=api, platform=platform, entry=entry
    )

    # First data fetch — raises ConfigEntryNotReady on failure
    await coordinator.async_config_entry_first_refresh()

    # Start the proactive 20-minute token refresh timer
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    async_register_relay(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: ZmodoCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()
    await async_stop_live_hubs(hass, entry.entry_id)

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
