"""Client for the new Zmodo platform API (user.zmodo.com).

Zmodo moved the web client to a JSON API with JWT bearer auth in Oct 2026.
Known endpoints (from browser DevTools on user.zmodo.com):

  POST /api/platform/v1/auth/login     {"email", "password"} (plain text)
       -> access_token (1 h), refresh_token (30 d), token_type "bearer"
  GET  /api/platform/v1/device         -> [ {id, serial, name, online, ...} ]
  GET  /api/platform/v1/alerts?limit=N -> [ {id, device_id, serial, reason, ts, has_image, ...} ]
  POST /api/gateway/sessions/live      {"serial", "relayToken": <access_token>}
       -> {session, playlist: wss://.../media/sessions/<id>/stream, audio}
  DELETE /api/gateway/sessions/<session> -> {"message": "This viewing session has ended."}
  GET  /api/platform/v1/alerts/<id>/image -> image bytes
  GET  /api/platform/v1/alerts/<id>/clip  -> video/mp4

Still unknown: the refresh endpoint and device settings.
Until the refresh call is captured we simply log in again shortly before
the access token expires.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

PLATFORM_BASE_URL = "https://user.zmodo.com"
LOGIN_PATH = "/api/platform/v1/auth/login"
DEVICES_PATH = "/api/platform/v1/device"
ALERTS_PATH = "/api/platform/v1/alerts"
LIVE_SESSION_PATH = "/api/gateway/sessions/live"
SESSION_PATH = "/api/gateway/sessions/{session}"
ALERT_MEDIA_PATH = "/api/platform/v1/alerts/{alert_id}/{kind}"
ALERT_MEDIA_KINDS = ("image", "clip")
_ALERT_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

# Renew the access token this many seconds before its JWT "exp".
TOKEN_RENEW_MARGIN = 300


class ZmodoPlatformError(Exception):
    """Non-auth failure talking to the platform API."""


class ZmodoPlatformAuthError(ZmodoPlatformError):
    """Credentials rejected by the platform API."""


def jwt_expiry(token: str) -> float:
    """Return the JWT "exp" claim (epoch seconds), or 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:  # noqa: BLE001
        return 0.0


async def _error_text(resp: aiohttp.ClientResponse, limit: int = 200) -> str:
    """Short, log-friendly error body (HTML error pages reduced to their title)."""
    try:
        text = await resp.text()
    except Exception:  # noqa: BLE001
        return ""
    title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if title:
        return title.group(1).strip()[:limit]
    return re.sub(r"\s+", " ", text).strip()[:limit]


def alert_media_path(alert_id: str, kind: str) -> str:
    """Return the API path for an alert's image or clip (validated)."""
    if kind not in ALERT_MEDIA_KINDS or not _ALERT_ID_RE.fullmatch(alert_id or ""):
        raise ValueError(f"Invalid alert media request: {alert_id!r}/{kind!r}")
    return ALERT_MEDIA_PATH.format(alert_id=alert_id, kind=kind)


class ZmodoPlatformApi:
    """Thin async client for user.zmodo.com."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        base_url: str = PLATFORM_BASE_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base = base_url.rstrip("/")
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: str | None = None
        # The login response's access_server block — streaming session identity
        # (token_id JWT, client_id, cid, user_conn hosts). Needed for cloud PTZ.
        self.access_server: dict[str, Any] = {}
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        """The shared aiohttp session (used by the PTZ WebSocket client)."""
        return self._session

    # -- auth ----------------------------------------------------------

    async def login(self) -> dict[str, Any]:
        """Log in with email/password and store the returned tokens."""
        async with self._session.post(
            f"{self._base}{LOGIN_PATH}",
            json={"email": self._email, "password": self._password},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (400, 401, 403):
                raise ZmodoPlatformAuthError(
                    f"Login rejected ({resp.status}): {await _error_text(resp)}"
                )
            if resp.status >= 300:
                raise ZmodoPlatformError(
                    f"Login failed ({resp.status}): {await _error_text(resp)}"
                )
            data = await resp.json(content_type=None)

        token = data.get("access_token")
        if not token:
            raise ZmodoPlatformAuthError(f"Login returned no access_token: {data}")
        self.access_token = token
        self.refresh_token = data.get("refresh_token")
        self.access_server = data.get("access_server") or {}
        self.user_id = self.access_server.get("client_id")
        self._expires_at = jwt_expiry(token) or (time.time() + 3600)
        return data

    async def ensure_token(self) -> str:
        """Return a valid access token, logging in again when near expiry."""
        async with self._token_lock:
            if (
                not self.access_token
                or time.time() > self._expires_at - TOKEN_RENEW_MARGIN
            ):
                await self.login()
            assert self.access_token
            return self.access_token

    # -- generic request -----------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        _retry: bool = True,
    ) -> Any:
        token = await self.ensure_token()
        async with self._session.request(
            method,
            f"{self._base}{path}",
            json=json_body,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401 and _retry:
                _LOGGER.debug("401 on %s; logging in again", path)
                self.access_token = None
                return await self._request(
                    method, path, json_body=json_body, params=params, _retry=False
                )
            if resp.status >= 300:
                raise ZmodoPlatformError(
                    f"{method} {path} failed ({resp.status}): {await _error_text(resp)}"
                )
            return await resp.json(content_type=None)

    # -- endpoints -----------------------------------------------------

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the account's cameras."""
        return await self._request("GET", DEVICES_PATH)

    async def get_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent alerts, newest first."""
        return await self._request("GET", ALERTS_PATH, params={"limit": limit})

    async def start_live_session(self, serial: str) -> dict[str, Any]:
        """Open a live session; returns {session, playlist (wss URL), audio}."""
        token = await self.ensure_token()
        return await self._request(
            "POST",
            LIVE_SESSION_PATH,
            json_body={"serial": serial, "relayToken": token},
        )

    async def end_live_session(self, session_id: str) -> None:
        """End a live session (what the website does when playback stops)."""
        if not session_id or not _ALERT_ID_RE.fullmatch(session_id):
            return
        token = await self.ensure_token()
        async with self._session.delete(
            f"{self._base}{SESSION_PATH.format(session=session_id)}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            _LOGGER.debug("Ended live session %s… (%s)", session_id[:8], resp.status)

    @asynccontextmanager
    async def open_media(
        self, path: str, headers: dict[str, str] | None = None
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """GET a binary resource with bearer auth; yields the open response.

        Retries once with a fresh login on 401. The caller checks status.
        """
        for attempt in range(2):
            token = await self.ensure_token()
            resp = await self._session.get(
                f"{self._base}{path}",
                headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30),
            )
            if resp.status == 401 and attempt == 0:
                resp.release()
                self.access_token = None
                continue
            try:
                yield resp
            finally:
                resp.release()
            return

    async def get_alert_image(self, alert_id: str) -> tuple[bytes, str]:
        """Return (bytes, content_type) of an alert's still image."""
        async with self.open_media(alert_media_path(alert_id, "image")) as resp:
            if resp.status >= 300:
                raise ZmodoPlatformError(
                    f"Alert image {alert_id} failed ({resp.status})"
                )
            return await resp.read(), resp.content_type or "image/jpeg"
