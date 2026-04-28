"""DataUpdateCoordinator for Zmodo — with proactive token refresh.

The Zmodo session token (GetSessionId) has a Max-Age of 1440 seconds
(24 minutes), as confirmed by MITM capture of the refresh_login response.
We refresh proactively every TOKEN_REFRESH_INTERVAL seconds so the token
is never stale when a data poll or stream URL is built.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZmodoApi, ZmodoApiError, ZmodoAuthError
from .const import (
    APP_MOP_HOSTS,
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

TOKEN_REFRESH_INTERVAL = 20 * 60  # 20 minutes — 4 min before the 24-min expiry


def build_alert_media_url(base: str, path: str, token: str) -> str:
    """Build a fully-authenticated URL for an alert thumbnail image.

    GET /storage/get_file?token=TOKEN&url=PATH
    """
    params = urlencode({"token": token, "url": path})
    return f"{base}/storage/get_file?{params}"


def build_alert_video_url(base: str, path: str, token: str, physical_id: str) -> str:
    """Build a fully-authenticated URL for an alert video clip.

    Video clips require three extra params beyond the image URL:
      transcoding=1   — always 1; tells the server to serve as MP4
      physical_id     — the camera's physical ID
      _file=alarm.mp4 — forces the filename/extension in the response
    """
    params = urlencode({
        "token": token,
        "url": path,
        "transcoding": "1",
        "physical_id": physical_id,
        "_file": "alarm.mp4",
    })
    return f"{base}/storage/get_file?{params}"



def build_device_pic_url(base: str, path: str, token: str, physical_id: str) -> str:
    """Build a fully-authenticated URL for a device product image.

    GET /storage/get_file?token=TOKEN&physical_id=ID&url=PATH
    The url param is the pic_url value returned by the storage_list endpoint.
    """
    params = urlencode({"token": token, "physical_id": physical_id, "url": path})
    return f"{base}/storage/get_file?{params}"


class ZmodoCoordinator(DataUpdateCoordinator):
    """Fetch devices and per-device latest alerts, refreshing the token proactively."""

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

        self._token: str = entry.data[CONF_TOKEN]
        self._login_cert: str = entry.data.get(CONF_LOGIN_CERT, "")
        self._client_uuid: str = entry.data.get(CONF_CLIENT_UUID, "")
        self._mng_addresses: list[str] = entry.data.get(CONF_MNG_ADDRESSES, [])
        self._alarm_addresses: list[str] = entry.data.get(CONF_ALARM_ADDRESSES, [])
        self._app_addresses: list[str] = entry.data.get(CONF_APP_ADDRESSES, [])
        self._token_refreshed_at: float = time.monotonic()
        self._refresh_unsub = None
        # Notification state tracked optimistically — not polled from the API
        self._notifications_on: bool = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Register the proactive token refresh timer."""
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
        """Return the current session token."""
        return self._token

    async def _refresh_token(self) -> bool:
        """Silently refresh the session token using the stored login_cert."""
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

        self._token = new_token
        self._login_cert = new_cert
        self._mng_addresses = new_mng
        self._alarm_addresses = new_alarm
        self._app_addresses = new_app
        self._token_refreshed_at = time.monotonic()

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
    # Helpers
    # ------------------------------------------------------------------

    def _alarm_base(self) -> str:
        """Return the first available alarm server base URL."""
        return self._alarm_addresses[0] if self._alarm_addresses else ""

    def _app_base(self) -> str:
        """Return the first available app server base URL."""
        return self._app_addresses[0] if self._app_addresses else APP_MOP_HOSTS[0]

    def alert_image_url(self, image_path: str) -> str:
        """Return a fully-authenticated URL for an alert thumbnail."""
        return build_alert_media_url(self._alarm_base(), image_path, self._token)

    def alert_video_url(self, video_path: str, physical_id: str) -> str:
        """Return a fully-authenticated URL for an alert video clip."""
        return build_alert_video_url(self._alarm_base(), video_path, self._token, physical_id)

    def device_pic_url(self, pic_path: str, physical_id: str) -> str:
        """Return a fully-authenticated URL for a device product image."""
        return build_device_pic_url(self._alarm_base(), pic_path, self._token, physical_id)

    async def async_set_notifications(self, enable: bool) -> None:
        """Toggle account-level push notifications on or off.

        State is tracked optimistically: we update _notifications_on before
        the API call so the switch flips immediately in the UI, then persist
        it so future polls carry the correct value without querying the API.
        """
        self._notifications_on = enable
        # Notify listeners immediately so the switch reflects the new state
        self.async_update_listeners()
        await self._api.set_notification_mode(
            self._app_base(), self._token, enable
        )

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    async def _fetch_devices(self) -> list[dict]:
        """Try each mng address; attempt one reactive refresh on auth failure."""
        last_exc: Exception | None = None

        for attempt in range(2):
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

    async def _fetch_alerts_for_device(self, physical_id: str) -> list[dict]:
        """Fetch the full 24h alert list for one device (newest-first).

        Tries each alarm address in order; returns an empty list on total
        failure so a single bad camera never blocks the whole update.
        """
        for addr in self._alarm_addresses:
            try:
                return await self._api.get_alerts_for_device(
                    addr, self._token, physical_id
                )
            except ZmodoApiError as exc:
                _LOGGER.debug(
                    "Alert fetch for %s failed on %s: %s",
                    physical_id, addr, exc,
                )
        return []

    async def _fetch_storage_list(self) -> dict[str, str]:
        """Fetch pic_url for each device from the storage list endpoint.

        Returns a dict of physical_id -> pic_url.
        Best-effort — returns empty dict on failure so it never blocks the poll.
        """
        for addr in self._mng_addresses:
            try:
                items = await self._api.get_storage_list(addr, self._token)
                return {
                    item["physical_id"]: item["pic_url"]
                    for item in items
                    if item.get("physical_id") and item.get("pic_url")
                }
            except ZmodoApiError as exc:
                _LOGGER.debug("Storage list failed for %s: %s", addr, exc)
        return {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll devices, alerts, and device images concurrently."""
        devices = await self._fetch_devices()

        # Fetch alerts and device images concurrently
        physical_ids = [d["physical_id"] for d in devices]
        alert_results, device_pics = await asyncio.gather(
            asyncio.gather(
                *[self._fetch_alerts_for_device(pid) for pid in physical_ids],
                return_exceptions=True,
            ),
            self._fetch_storage_list(),
        )

        # Derive both latest alert and 24h count from the same list
        latest_alerts: dict[str, dict | None] = {}
        alert_counts: dict[str, int] = {}
        for pid, result in zip(physical_ids, alert_results):
            if isinstance(result, Exception):
                _LOGGER.debug("Alert fetch for %s raised: %s", pid, result)
                alerts_list: list[dict] = []
            else:
                alerts_list = result  # type: ignore[assignment]
            latest_alerts[pid] = alerts_list[0] if alerts_list else None
            alert_counts[pid] = len(alerts_list)

        return {
            "devices": {d["physical_id"]: d for d in devices},
            "latest_alerts": latest_alerts,
            "alert_counts": alert_counts,
            "device_pics": device_pics,  # physical_id -> pic_url
            # Carried from in-memory state — updated only when the switch is toggled
            "notifications_on": self._notifications_on,
        }
