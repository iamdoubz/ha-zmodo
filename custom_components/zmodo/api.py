"""Zmodo API client."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

import aiohttp

from .const import (
    ALARM_SEARCH_PATH,
    ALERT_WINDOW_SECONDS,
    API_APP_LOGIN_PATH,
    API_REFRESH_LOGIN_PATH,
    APP_MOP_HOSTS,
    DEVICE_LIST_PATH,
    LOGIN_APP_VERSION,
    LOGIN_CID,
    LOGIN_CLIENT,
    LOGIN_CLIENT_VERSION,
    LOGIN_LANGUAGE,
    LOGIN_PLATFORM,
)

_LOGGER = logging.getLogger(__name__)


def md5_hash(value: str) -> str:
    """Return the MD5 hex digest of a string."""
    return hashlib.md5(value.encode()).hexdigest()


def stable_client_uuid() -> str:
    """Return a stable per-install UUID derived from the machine MAC address.

    The iOS app sends a persistent device UUID; we generate one from the
    machine's MAC address so it stays the same across HA restarts.
    """
    return uuid.uuid5(uuid.NAMESPACE_DNS, str(uuid.getnode())).hex


def _app_info() -> str:
    """Build the app_info JSON string the iOS app sends on login."""
    return json.dumps(
        {
            "version_name": LOGIN_APP_VERSION,
            "SYS_SDK": "18.0",
            "SYS_RELEASE": "18.0",
            "MODEL": "Home Assistant",
        },
        separators=(",", ":"),
    )


class ZmodoAuthError(Exception):
    """Authentication error (wrong password, expired cert, account locked…)."""


class ZmodoTokenExpiredError(ZmodoAuthError):
    """Token has expired and must be refreshed."""


class ZmodoApiError(Exception):
    """General API error (server-side failure, unexpected response…)."""


class ZmodoApi:
    """Zmodo / MeShare cloud API client (app-style, no captcha)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(
        self,
        url: str,
        payload: dict,
        token: str | None = None,
        timeout: int = 10,
    ) -> dict[str, Any]:
        """POST url-encoded form data, optionally with a token Cookie."""
        headers: dict[str, str] = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if token:
            headers["Cookie"] = f"token={token}"

        async with self._session.post(
            url,
            data=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(
        self,
        email: str,
        password_plain: str,
        client_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Full login using email + password (app-style, no captcha).

        Tries each host in APP_MOP_HOSTS in order.

        Returns the full API response dict, which includes:
          - token        : short-lived session token
          - login_cert   : long-lived cert used to refresh the token
          - data.id      : user ID
          - host_list    : server addresses for subsequent calls

        Raises:
            ZmodoAuthError:   bad credentials.
            ZmodoApiError:    server-side error.
            aiohttp.ClientError: network failure on all hosts.
        """
        if client_uuid is None:
            client_uuid = stable_client_uuid()

        payload = {
            "app_info": _app_info(),
            "app_version": LOGIN_APP_VERSION,
            "cid": LOGIN_CID,
            "client": LOGIN_CLIENT,
            "client_uuid": client_uuid,
            "client_version": LOGIN_CLIENT_VERSION,
            "email": email,
            "language": LOGIN_LANGUAGE,
            "offset_second": str(int(-time.timezone)),
            "password": md5_hash(password_plain),
            "platform": LOGIN_PLATFORM,
        }

        last_exc: Exception | None = None
        for host in APP_MOP_HOSTS:
            url = f"{host}{API_APP_LOGIN_PATH}"
            try:
                data = await self._post(url, payload)
                if data.get("result") != "ok":
                    msg = data.get("msg") or data.get("result") or "unknown"
                    raise ZmodoAuthError(f"Login failed: {msg}")
                data["_login_host"] = host
                return data
            except ZmodoAuthError:
                raise
            except Exception as exc:
                _LOGGER.debug("Login attempt to %s failed: %s", host, exc)
                last_exc = exc

        raise aiohttp.ClientError(
            f"All login hosts failed. Last error: {last_exc}"
        ) from last_exc

    async def refresh_login(
        self,
        current_token: str,
        login_cert: str,
        client_uuid: str,
    ) -> dict[str, Any]:
        """Silently refresh the session token using the stored login_cert.

        This mirrors exactly what the iOS app does periodically:
          POST /user/refresh_login
          Cookie: token=<current_token>
          Body:   client_uuid=…&client_version=…&language=en&login_cert=…

        Returns the full API response (same shape as login()), so the
        caller can update token, login_cert, and host_list from it.

        Raises:
            ZmodoAuthError:      cert invalid / expired — full re-login needed.
            ZmodoApiError:       server-side failure.
            aiohttp.ClientError: network failure on all hosts.
        """
        payload = {
            "client_uuid": client_uuid,
            "client_version": LOGIN_CLIENT_VERSION,
            "language": LOGIN_LANGUAGE,
            "login_cert": login_cert,
        }

        last_exc: Exception | None = None
        for host in APP_MOP_HOSTS:
            url = f"{host}{API_REFRESH_LOGIN_PATH}"
            try:
                data = await self._post(url, payload, token=current_token)

                if data.get("result") != "ok":
                    msg = data.get("msg") or data.get("result") or "unknown"
                    _LOGGER.debug("refresh_login non-ok from %s: %s", host, msg)
                    raise ZmodoAuthError(f"Token refresh failed: {msg}")

                _LOGGER.debug("Token refreshed successfully via %s", host)
                data["_login_host"] = host
                return data

            except ZmodoAuthError:
                raise
            except Exception as exc:
                _LOGGER.debug("refresh_login attempt to %s failed: %s", host, exc)
                last_exc = exc

        raise aiohttp.ClientError(
            f"All refresh hosts failed. Last error: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Authenticated data calls
    # ------------------------------------------------------------------

    async def get_devices(self, mng_address: str, token: str) -> list[dict[str, Any]]:
        """Return the full device list for the account."""
        url = f"{mng_address}{DEVICE_LIST_PATH}"
        data = await self._post(
            url, {"token": token, "start": 0, "count": 999}, timeout=15
        )
        if data.get("result") != "ok":
            raise ZmodoApiError(f"Device list failed: {data}")
        return data.get("data", [])

    async def get_alerts(
        self,
        alarm_address: str,
        token: str,
        window_seconds: int = ALERT_WINDOW_SECONDS,
    ) -> list[dict[str, Any]]:
        """Return motion alerts from the last *window_seconds* seconds."""
        now = int(time.time())
        url = f"{alarm_address}{ALARM_SEARCH_PATH}"
        params = {
            "token": token,
            "max_time": now + 60,
            "min_time": now - window_seconds,
            "count": 999,
            "main_type": 1,
        }
        async with self._session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

        if data.get("result") != "ok":
            raise ZmodoApiError(f"Alert fetch failed: {data}")
        return data.get("data", [])

    async def get_latest_alert_for_device(
        self,
        alarm_address: str,
        token: str,
        physical_id: str,
        window_seconds: int = ALERT_WINDOW_SECONDS,
    ) -> dict[str, Any] | None:
        """Return the single most recent alert for one specific device.

        Passes physical_id as a query param (as the mobile app does), and
        requests only count=1 so the server does the heavy lifting.
        Returns the alert dict or None if there are no recent alerts.
        """
        now = int(time.time())
        url = f"{alarm_address}{ALARM_SEARCH_PATH}"
        params = {
            "token": token,
            "max_time": now + 60,
            "min_time": now - window_seconds,
            "count": 1,
            "main_type": 1,
            "physical_id": physical_id,
        }
        async with self._session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

        if data.get("result") != "ok":
            raise ZmodoApiError(f"Alert fetch for {physical_id} failed: {data}")

        items = data.get("data", [])
        return items[0] if items else None

