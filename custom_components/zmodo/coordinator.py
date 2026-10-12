"""DataUpdateCoordinator for Zmodo.

Hybrid model (Oct 2026):

* **Platform API** (user.zmodo.com, JWT) — source of truth for the camera
  list, online state, motion alerts and live video. Required.
* **Legacy MeShare app API** (token + login_cert) — optional. Only used for
  the settings Zmodo has not exposed on the new platform yet (microphone,
  volume, frame rate, night vision, notifications) and device pictures. When it fails those entities go unavailable and the
  integration backs off for LEGACY_RETRY_SECONDS before trying again.

Devices are keyed by serial number, which matches the legacy physical_id,
so existing entity unique IDs are preserved.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZmodoApi, ZmodoApiError, ZmodoAuthError, stable_client_uuid
from .const import (
    ALERT_COUNT_WINDOW_SECONDS,
    ALERT_FETCH_LIMIT,
    APP_MOP_HOSTS,
    CONF_ALARM_ADDRESSES,
    CONF_APP_ADDRESSES,
    CONF_CLIENT_UUID,
    CONF_LOGIN_CERT,
    CONF_MNG_ADDRESSES,
    CONF_TOKEN,
    DOMAIN,
    LEGACY_RETRY_SECONDS,
    UPDATE_INTERVAL,
)
from .platform_api import ZmodoPlatformApi, ZmodoPlatformAuthError, ZmodoPlatformError

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_INTERVAL = 20 * 60  # 20 minutes — 4 min before the 24-min expiry


def build_device_pic_url(base: str, path: str, token: str, physical_id: str) -> str:
    """Build a fully-authenticated URL for a device product image.

    GET /storage/get_file?token=TOKEN&physical_id=ID&url=PATH
    The url param is the pic_url value returned by the storage_list endpoint.
    """
    params = urlencode({"token": token, "physical_id": physical_id, "url": path})
    return f"{base}/storage/get_file?{params}"


class ZmodoCoordinator(DataUpdateCoordinator):
    """Poll the platform API, plus the legacy API for settings when it works."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ZmodoApi,
        platform: ZmodoPlatformApi,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._api = api
        self.platform = platform
        self._entry = entry

        # Legacy MeShare session (optional)
        self._legacy_ok = False
        self._legacy_retry_at = 0.0
        self._legacy_ids: dict[str, str] = {}  # serial -> legacy physical_id
        self._token: str = entry.data.get(CONF_TOKEN, "")
        self._login_cert: str = entry.data.get(CONF_LOGIN_CERT, "")
        self._client_uuid: str = entry.data.get(CONF_CLIENT_UUID, "")
        self._mng_addresses: list[str] = entry.data.get(CONF_MNG_ADDRESSES, [])
        self._alarm_addresses: list[str] = entry.data.get(CONF_ALARM_ADDRESSES, [])
        self._app_addresses: list[str] = entry.data.get(CONF_APP_ADDRESSES, [])
        self._token_refreshed_at: float = time.monotonic()
        self._refresh_unsub = None
        # Notification state tracked optimistically — not polled from the API
        # Real state fetched from server on first poll; True until then
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
        if not self._legacy_ok:
            return  # legacy recovery is handled (with back-off) by the poll
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
        """Return the current legacy session token."""
        return self._token

    @property
    def entry_id(self) -> str:
        """Config entry ID (used to build relay paths)."""
        return self._entry.entry_id

    @property
    def legacy_available(self) -> bool:
        """True while the legacy MeShare session is working."""
        return self._legacy_ok

    def legacy_device_available(self, serial: str) -> bool:
        """True when settings for this camera can be read/changed."""
        return self._legacy_ok and serial in self._legacy_ids

    def _legacy_id(self, serial: str) -> str:
        """Map a serial to the legacy physical_id (normally identical)."""
        if not self._legacy_ok:
            raise HomeAssistantError(
                "Zmodo legacy API is unavailable; this setting cannot be changed right now"
            )
        return self._legacy_ids.get(serial, serial)

    async def _refresh_token(self) -> bool:
        """Refresh the session token, falling back to a full password login.

        First tries the silent refresh_login with the stored login_cert. If the
        server rejects it (e.g. result 1300 — cert revoked/expired) or there is
        no cert, performs a full login with the stored email/password so the
        integration recovers on its own instead of looping on a dead session.
        """
        if self._login_cert and self._client_uuid:
            try:
                data = await self._api.refresh_login(
                    current_token=self._token,
                    login_cert=self._login_cert,
                    client_uuid=self._client_uuid,
                )
            except ZmodoAuthError as err:
                _LOGGER.info(
                    "Token refresh rejected by server (%s); falling back to full login",
                    err,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Token refresh network error: %s", err)
                return False
            else:
                self._apply_session(data)
                _LOGGER.debug("Zmodo token refreshed and persisted to config entry")
                return True
        else:
            _LOGGER.debug("No login_cert stored; attempting full login")

        return await self._full_login()

    async def _full_login(self) -> bool:
        """Log in again with the stored email/password and persist the new session."""
        email = self._entry.data.get("email")
        password = self._entry.data.get("password")
        if not email or not password:
            _LOGGER.warning("No stored credentials; cannot re-login to Zmodo")
            return False

        if not self._client_uuid:
            self._client_uuid = stable_client_uuid()

        try:
            data = await self._api.login(
                email=email,
                password_plain=password,
                client_uuid=self._client_uuid,
            )
        except ZmodoAuthError as err:
            _LOGGER.warning("Zmodo re-login rejected by server: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zmodo re-login failed: %s", err)
            return False

        self._apply_session(data)
        _LOGGER.info("Zmodo session re-established via full login")
        return True

    def _apply_session(self, data: dict[str, Any]) -> None:
        """Store token/cert/host lists from a login or refresh response."""
        host_list = data.get("host_list") or {}
        self._token = data.get("token", self._token)
        self._login_cert = data.get("login_cert", self._login_cert)
        self._mng_addresses = host_list.get("mng_address") or self._mng_addresses
        self._alarm_addresses = host_list.get("alarm_address") or self._alarm_addresses
        self._app_addresses = host_list.get("app_address") or self._app_addresses
        self._token_refreshed_at = time.monotonic()

        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_TOKEN: self._token,
                CONF_LOGIN_CERT: self._login_cert,
                CONF_CLIENT_UUID: self._client_uuid,
                CONF_MNG_ADDRESSES: self._mng_addresses,
                CONF_ALARM_ADDRESSES: self._alarm_addresses,
                CONF_APP_ADDRESSES: self._app_addresses,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _alarm_base(self) -> str:
        """Return the first available alarm server base URL."""
        return self._alarm_addresses[0] if self._alarm_addresses else ""

    def _app_base(self) -> str:
        """Return the first available app server base URL."""
        return self._app_addresses[0] if self._app_addresses else APP_MOP_HOSTS[0]

    def device_pic_url(self, pic_path: str, physical_id: str) -> str:
        """Return a fully-authenticated URL for a device product image."""
        return build_device_pic_url(
            self._alarm_base(), pic_path, self._token,
            self._legacy_ids.get(physical_id, physical_id),
        )

    async def async_set_device_mute(self, physical_id: str, mic_active: bool) -> None:
        """Set microphone state for a device and update coordinator data optimistically.

        mic_active=True  → microphone on  (mute field = "1" in device data)
        mic_active=False → microphone off (mute field = "0" in device data)
        """
        legacy_id = self._legacy_id(physical_id)
        devices = self.data.get("devices", {})
        if physical_id in devices:
            # mute field in device data: "1"=mic on, "0"=mic off
            devices[physical_id]["mute"] = "1" if mic_active else "0"
        self.async_update_listeners()

        device_type = devices.get(physical_id, {}).get("device_type", "22")
        for addr in self._mng_addresses:
            try:
                await self._api.set_device_mute(
                    addr, self._token, legacy_id, device_type, mic_active
                )
                return
            except ZmodoApiError as exc:
                _LOGGER.debug("set_device_mute failed for %s on %s: %s", physical_id, addr, exc)
        _LOGGER.warning("set_device_mute failed on all management addresses for %s", physical_id)

    async def async_set_device_nightvision(self, physical_id: str, mode: int) -> None:
        """Set night vision mode and update coordinator data optimistically."""
        legacy_id = self._legacy_id(physical_id)
        devices = self.data.get("devices", {})
        if physical_id in devices:
            devices[physical_id]["nightvision"] = str(mode)
        self.async_update_listeners()

        for addr in self._mng_addresses:
            try:
                await self._api.set_device_nightvision(addr, self._token, legacy_id, mode)
                return
            except ZmodoApiError as exc:
                _LOGGER.debug("set_device_nightvision failed for %s on %s: %s", physical_id, addr, exc)
        _LOGGER.warning("set_device_nightvision failed on all management addresses for %s", physical_id)

    async def async_set_device_night_level(self, physical_id: str, level: int) -> None:
        """Set night vision sensitivity and update coordinator data optimistically."""
        legacy_id = self._legacy_id(physical_id)
        devices = self.data.get("devices", {})
        if physical_id in devices:
            devices[physical_id]["night_level"] = str(level)
        self.async_update_listeners()

        for addr in self._mng_addresses:
            try:
                await self._api.set_device_night_level(addr, self._token, legacy_id, level)
                return
            except ZmodoApiError as exc:
                _LOGGER.debug("set_device_night_level failed for %s on %s: %s", physical_id, addr, exc)
        _LOGGER.warning("set_device_night_level failed on all management addresses for %s", physical_id)

    async def async_set_device_frame_rate(self, physical_id: str, frame_rate: int) -> None:
        """Set the frame rate for a device and update coordinator data optimistically."""
        legacy_id = self._legacy_id(physical_id)
        devices = self.data.get("devices", {})
        if physical_id in devices:
            devices[physical_id]["frame_rate"] = str(frame_rate)
        self.async_update_listeners()

        for addr in self._mng_addresses:
            try:
                await self._api.set_device_frame_rate(addr, self._token, legacy_id, frame_rate)
                return
            except ZmodoApiError as exc:
                _LOGGER.debug("set_device_frame_rate failed for %s on %s: %s", physical_id, addr, exc)
        _LOGGER.warning("set_device_frame_rate failed on all management addresses for %s", physical_id)

    async def async_set_device_volume(self, physical_id: str, volume: int) -> None:
        """Set the speaker volume for a device and update coordinator data optimistically."""
        legacy_id = self._legacy_id(physical_id)
        # Optimistic update so the slider snaps to the new value immediately
        devices = self.data.get("devices", {})
        if physical_id in devices:
            devices[physical_id]["device_volume"] = str(volume)
        self.async_update_listeners()

        for addr in self._mng_addresses:
            try:
                await self._api.set_device_volume(addr, self._token, legacy_id, volume)
                return
            except ZmodoApiError as exc:
                _LOGGER.debug("set_device_volume failed for %s on %s: %s", physical_id, addr, exc)
        _LOGGER.warning("set_device_volume failed on all management addresses for %s", physical_id)

    async def async_set_notifications(self, enable: bool) -> None:
        """Toggle account-level push notifications on or off.

        State is tracked optimistically: we update _notifications_on before
        the API call so the switch flips immediately in the UI, then persist
        it so future polls carry the correct value without querying the API.
        """
        self._legacy_id("")  # raises if the legacy session is down
        self._notifications_on = enable
        # Notify listeners immediately so the switch reflects the new state
        self.async_update_listeners()
        await self._api.set_notification_mode(
            self._app_base(), self._token, enable
        )

    # ------------------------------------------------------------------
    # Legacy data fetch (best effort)
    # ------------------------------------------------------------------

    async def _fetch_legacy_devices(self) -> list[dict] | None:
        """Return the legacy device list, or None if the legacy API is down."""
        if not self._mng_addresses and not await self._refresh_token():
            return None

        last_exc: Exception | None = None
        for attempt in range(2):
            for addr in self._mng_addresses:
                try:
                    return await self._api.get_devices(addr, self._token)
                except (ZmodoApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    _LOGGER.debug("Legacy device fetch failed for %s: %s", addr, exc)
                    last_exc = exc
            if attempt == 0 and not await self._refresh_token():
                break

        _LOGGER.debug("Legacy device list unavailable: %s", last_exc)
        return None

    async def _fetch_storage_list(self) -> dict[str, str]:
        """Fetch pic_url for each device from the storage list endpoint.

        Returns a dict of legacy physical_id -> pic_url (best effort).
        """
        for addr in self._mng_addresses:
            try:
                items = await self._api.get_storage_list(addr, self._token)
                return {
                    item["physical_id"]: item["pic_url"]
                    for item in items
                    if item.get("physical_id") and item.get("pic_url")
                }
            except (ZmodoApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.debug("Storage list failed for %s: %s", addr, exc)
        return {}

    async def _fetch_notification_mode(self) -> bool:
        """Fetch the current notification mode; keep last state on failure."""
        for addr in self._app_addresses or APP_MOP_HOSTS:
            try:
                return await self._api.get_notification_mode(addr, self._token)
            except (ZmodoApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.debug("Notification mode fetch failed for %s: %s", addr, exc)
        return self._notifications_on

    async def _fetch_legacy(self) -> dict[str, Any] | None:
        """Fetch everything the legacy API still provides, or None if it is down."""
        if self._legacy_retry_at and time.monotonic() < self._legacy_retry_at:
            return None

        devices = await self._fetch_legacy_devices()
        if devices is None:
            if self._legacy_ok or not self._legacy_retry_at:
                _LOGGER.warning(
                    "Zmodo legacy API unavailable; settings entities disabled, "
                    "retrying in %d minutes",
                    LEGACY_RETRY_SECONDS // 60,
                )
            self._legacy_ok = False
            self._legacy_retry_at = time.monotonic() + LEGACY_RETRY_SECONDS
            return None

        if not self._legacy_ok:
            _LOGGER.info("Zmodo legacy API available; settings entities enabled")
        self._legacy_ok = True
        self._legacy_retry_at = 0.0

        device_pics, notifications_on = await asyncio.gather(
            self._fetch_storage_list(),
            self._fetch_notification_mode(),
        )
        self._notifications_on = notifications_on
        return {"devices": devices, "device_pics": device_pics}

    # ------------------------------------------------------------------
    # Main poll
    # ------------------------------------------------------------------

    async def _fetch_platform(self) -> tuple[list[dict], list[dict]]:
        """Fetch devices and recent alerts from the platform API."""
        try:
            await self.platform.ensure_token()
            devices, alerts = await asyncio.gather(
                self.platform.get_devices(),
                self.platform.get_alerts(ALERT_FETCH_LIMIT),
            )
        except ZmodoPlatformAuthError as err:
            raise UpdateFailed(f"Zmodo login rejected: {err}") from err
        except (ZmodoPlatformError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Zmodo platform API error: {err}") from err
        return devices or [], alerts or []

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the platform API, then merge in legacy settings if available."""
        platform_devices, alerts = await self._fetch_platform()
        legacy = await self._fetch_legacy()

        legacy_devices = legacy["devices"] if legacy else []
        by_id = {d.get("physical_id"): d for d in legacy_devices}
        by_name = {d.get("device_name"): d for d in legacy_devices}

        devices: dict[str, dict[str, Any]] = {}
        legacy_ids: dict[str, str] = {}
        for pd in platform_devices:
            serial = pd.get("serial")
            if not serial:
                continue
            ld = by_id.get(serial) or by_name.get(pd.get("name")) or {}
            if ld.get("physical_id"):
                legacy_ids[serial] = ld["physical_id"]
            dev = dict(ld)
            dev.update(
                physical_id=serial,
                serial=serial,
                platform_id=pd.get("id"),
                device_name=pd.get("name") or ld.get("device_name") or serial,
                device_online="1" if pd.get("online") else "0",
                aes_key=pd.get("aes_key") or ld.get("aes_key", ""),
                product_id=pd.get("product_id"),
                device_channel=pd.get("device_channel", ld.get("device_channel", 1)),
                audio=pd.get("audio"),
            )
            devices[serial] = dev
        self._legacy_ids = legacy_ids

        # Platform alerts: newest first; derive latest time + 24h count.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ALERT_COUNT_WINDOW_SECONDS)
        platform_alerts: dict[str, list[dict]] = {s: [] for s in devices}
        for alert in alerts:
            serial = alert.get("serial")
            if serial in platform_alerts:
                platform_alerts[serial].append(alert)
        last_alert_time: dict[str, datetime | None] = {}
        alert_counts: dict[str, int] = {}
        for serial, items in platform_alerts.items():
            times = [t for t in (self._parse_ts(a.get("ts")) for a in items) if t]
            last_alert_time[serial] = max(times) if times else None
            alert_counts[serial] = sum(1 for t in times if t >= cutoff)

        # Legacy device pictures, re-keyed by serial
        to_serial = {lid: serial for serial, lid in legacy_ids.items()}
        device_pics: dict[str, str] = {}
        if legacy:
            for lid, pic in legacy["device_pics"].items():
                if lid in to_serial:
                    device_pics[to_serial[lid]] = pic

        return {
            "devices": devices,
            "platform_alerts": platform_alerts,
            "last_alert_time": last_alert_time,
            "alert_counts": alert_counts,
            "latest_alerts": {s: (a[0] if a else None) for s, a in platform_alerts.items()},
            "device_pics": device_pics,
            "notifications_on": self._notifications_on,
        }
