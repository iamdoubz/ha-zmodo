"""DataUpdateCoordinator for Zmodo — with proactive token refresh.

The Zmodo session token (GetSessionId) has a Max-Age of 1440 seconds
(24 minutes), as confirmed by MITM capture of the refresh_login response.
We refresh proactively every TOKEN_REFRESH_INTERVAL seconds so the token
is never stale when a data poll or stream URL is built.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZmodoApi, ZmodoApiError, ZmodoAuthError
from .const import (
    CONF_ALARM_ADDRESSES,
    CONF_APP_ADDRESSES,
    CONF_CLIENT_UUID,
    CONF_LOGIN_CERT,
    CONF_MNG_ADDRESSES,
    CONF_TOKEN,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Refresh 4 minutes before the 24-minute expiry window to give plenty of headroom
TOKEN_REFRESH_INTERVAL = 20 * 60  # 20 minutes in seconds


class ZmodoCoordinator(DataUpdateCoordinator):
    """Fetch devices and alerts, refreshing the token proactively."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ZmodoApi,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._api = api
        self._entry = entry

        # Working copies updated whenever a refresh succeeds
        self._token: str = entry.data[CONF_TOKEN]
        self._login_cert: str = entry.data.get(CONF_LOGIN_CERT, "")
        self._client_uuid: str = entry.data.get(CONF_CLIENT_UUID, "")
        self._mng_addresses: list[str] = entry.data.get(CONF_MNG_ADDRESSES, [])
        self._alarm_addresses: list[str] = entry.data.get(CONF_ALARM_ADDRESSES, [])

        # Track when the token was last refreshed
        self._token_refreshed_at: float = time.monotonic()

        # Will hold the cancel callback for the scheduled refresh
        self._refresh_unsub = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Register the proactive token refresh timer.

        Call this once after the coordinator is created and the first
        refresh has succeeded.
        """
        self._refresh_unsub = async_track_time_interval(
            self.hass,
            self._handle_scheduled_refresh,
            timedelta(seconds=TOKEN_REFRESH_INTERVAL),
        )
        _LOGGER.debug(
            "Zmodo token refresh scheduled every %d minutes",
            TOKEN_REFRESH_INTERVAL // 60,
        )

    async def async_shutdown(self) -> None:
        """Cancel the token refresh timer on unload."""
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None

    async def _handle_scheduled_refresh(self, _now=None) -> None:
        """Callback fired by async_track_time_interval every 20 minutes."""
        _LOGGER.debug("Proactive Zmodo token refresh triggered")
        success = await self._refresh_token()
        if not success:
            _LOGGER.warning(
                "Proactive token refresh failed; next data poll will attempt recovery"
            )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    @property
    def token(self) -> str:
        """Return the current (possibly freshly refreshed) session token."""
        return self._token

    async def _refresh_token(self) -> bool:
        """Silently refresh the session token using the stored login_cert.

        Returns True on success and persists the new token to the config entry.
        Returns False if the cert is missing, invalid, or the call fails.
        """
        if not self._login_cert or not self._client_uuid:
            _LOGGER.debug("No login_cert stored; cannot refresh token silently")
            return False

        try:
            data = await self._api.refresh_login(
                current_token=self._token,
                login_cert=self._login_cert,
                client_uuid=self._client_uuid,
            )
        except ZmodoAuthError as err:
            _LOGGER.warning("Token refresh rejected by server: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Token refresh network error: %s", err)
            return False

        new_token = data.get("token", self._token)
        new_cert = data.get("login_cert", self._login_cert)
        host_list = data.get("host_list", {})
        new_mng = host_list.get("mng_address", self._mng_addresses)
        new_alarm = host_list.get("alarm_address", self._alarm_addresses)
        new_app = host_list.get("app_address", self._entry.data.get(CONF_APP_ADDRESSES, []))

        # Update working state
        self._token = new_token
        self._login_cert = new_cert
        self._mng_addresses = new_mng
        self._alarm_addresses = new_alarm
        self._token_refreshed_at = time.monotonic()

        # Persist to config entry so the token survives an HA restart
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_TOKEN: new_token,
                CONF_LOGIN_CERT: new_cert,
                CONF_MNG_ADDRESSES: new_mng,
                CONF_ALARM_ADDRESSES: new_alarm,
                CONF_APP_ADDRESSES: new_app,
            },
        )

        _LOGGER.debug("Zmodo token refreshed and persisted to config entry")
        return True

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    async def _fetch_devices(self) -> list[dict]:
        """Try each mng address; attempt one reactive refresh on auth failure."""
        last_exc: Exception | None = None

        for attempt in range(2):  # attempt 0 = normal; attempt 1 = post-refresh
            for addr in self._mng_addresses:
                try:
                    return await self._api.get_devices(addr, self._token)
                except ZmodoApiError as exc:
                    _LOGGER.debug("Device fetch failed for %s: %s", addr, exc)
                    last_exc = exc

            if attempt == 0:
                _LOGGER.info(
                    "Device list failed on all addresses; attempting reactive token refresh"
                )
                refreshed = await self._refresh_token()
                if not refreshed:
                    break

        raise UpdateFailed(
            f"All management addresses failed: {last_exc}"
        ) from last_exc

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll devices and alerts."""
        devices = await self._fetch_devices()

        alerts: list[dict] = []
        for addr in self._alarm_addresses:
            try:
                alerts = await self._api.get_alerts(addr, self._token)
                break
            except ZmodoApiError as exc:
                _LOGGER.debug("Alert fetch failed for %s: %s", addr, exc)

        return {
            "devices": {d["physical_id"]: d for d in devices},
            "alerts": alerts,
        }
