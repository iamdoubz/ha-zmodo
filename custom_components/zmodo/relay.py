"""Local HTTP relays for Zmodo media.

The platform API authenticates with a Bearer header, which neither ffmpeg
(via a camera's stream_source) nor the HA frontend can send. These views sit
in between:

* ``/api/zmodo/live/<entry_id>/<serial>`` — live stream, decrypted to FLV.
* ``/api/zmodo/alert/<entry_id>/<alert_id>/<image|clip>`` — alert still or
  MP4 clip, proxied with Range support so the clip is seekable.

Access is allowed for authenticated HA requests (e.g. a companion-app
notification using the relative path) or with the per-run secret key used
by loopback consumers such as ffmpeg.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .platform_api import alert_media_path
from .stream import LiveHub

_LOGGER = logging.getLogger(__name__)

RELAY_KEY = f"{DOMAIN}_relay_key"
RELAY_REGISTERED = f"{DOMAIN}_relay_registered"
HUBS = f"{DOMAIN}_live_hubs"
_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                 "Last-Modified", "ETag")


def _relay_key(hass: HomeAssistant) -> str:
    """Per-run secret that authorises loopback relay URLs."""
    if RELAY_KEY not in hass.data:
        hass.data[RELAY_KEY] = secrets.token_urlsafe(32)
    return hass.data[RELAY_KEY]


def async_register_relay(hass: HomeAssistant) -> None:
    """Register the relay views once per HA run."""
    if hass.data.get(RELAY_REGISTERED):
        return
    hass.http.register_view(ZmodoLiveRelayView(hass))
    hass.http.register_view(ZmodoAlertMediaView(hass))
    hass.data[RELAY_REGISTERED] = True


def get_live_hub(hass: HomeAssistant, coordinator, serial: str, aes_key: str) -> LiveHub:
    """Return the shared live hub for one camera (one Zmodo session for all viewers)."""
    hubs: dict[tuple[str, str], LiveHub] = hass.data.setdefault(HUBS, {})
    key = (coordinator.entry_id, serial)
    hub = hubs.get(key)
    if hub is None:
        hub = LiveHub(
            async_get_clientsession(hass),
            coordinator.platform,
            serial,
            aes_key,
            create_task=lambda coro: hass.async_create_background_task(
                coro, f"zmodo live {serial}"
            ),
            local_clock=_local_seconds,
        )
        hubs[key] = hub
    return hub


def _local_seconds() -> float:
    now = dt_util.now()
    return now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6


def find_live_hub(hass: HomeAssistant, entry_id: str, serial: str) -> LiveHub | None:
    """Return the camera's hub if one exists (no side effects)."""
    return hass.data.get(HUBS, {}).get((entry_id, serial))


async def async_stop_live_hubs(hass: HomeAssistant, entry_id: str) -> None:
    """Stop every live hub of a config entry (ends their Zmodo sessions)."""
    hubs: dict[tuple[str, str], LiveHub] = hass.data.get(HUBS, {})
    for key in [k for k in hubs if k[0] == entry_id]:
        await hubs.pop(key).async_stop()


def _loopback_base(hass: HomeAssistant) -> str:
    api = hass.config.api
    scheme = "https" if api and api.use_ssl else "http"
    port = api.port if api else 8123
    return f"{scheme}://127.0.0.1:{port}"


def relay_url(hass: HomeAssistant, entry_id: str, serial: str) -> str:
    """Loopback URL of a camera's live relay (for ffmpeg/PyAV)."""
    return (
        f"{_loopback_base(hass)}/api/zmodo/live/{entry_id}/"
        f"{quote(serial, safe='')}?key={_relay_key(hass)}"
    )


def alert_media_relay_path(entry_id: str, alert_id: str, kind: str) -> str:
    """Relative, HA-authenticated path to an alert's image or clip."""
    return f"/api/zmodo/alert/{entry_id}/{quote(alert_id, safe='')}/{kind}"


def alert_media_loopback_url(
    hass: HomeAssistant, entry_id: str, alert_id: str, kind: str
) -> str:
    """Loopback URL of an alert's image or clip (for ffmpeg/PyAV)."""
    return (
        f"{_loopback_base(hass)}{alert_media_relay_path(entry_id, alert_id, kind)}"
        f"?key={_relay_key(hass)}"
    )


def _allowed(hass: HomeAssistant, request: web.Request) -> bool:
    if request.get(KEY_AUTHENTICATED):
        return True
    return hmac.compare_digest(request.query.get("key", ""), _relay_key(hass))


class ZmodoLiveRelayView(HomeAssistantView):
    """Serve a camera's live stream as FLV over HTTP."""

    url = "/api/zmodo/live/{entry_id}/{serial}"
    name = "api:zmodo:live"
    requires_auth = False  # checked in _allowed()

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, serial: str
    ) -> web.StreamResponse:
        if not _allowed(self.hass, request):
            return web.Response(status=401)

        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None or not coordinator.data:
            return web.Response(status=404)
        device = coordinator.data["devices"].get(serial)
        if device is None:
            return web.Response(status=404)

        hub = get_live_hub(self.hass, coordinator, serial, device.get("aes_key", ""))
        stream = hub.stream()
        async with contextlib.aclosing(stream):
            # Wait for real video before answering, so a failed camera
            # session is a clean 503 rather than an empty/invalid FLV.
            try:
                first = await anext(stream)
            except (StopAsyncIteration, asyncio.TimeoutError):
                return web.Response(status=503, text="Camera stream unavailable")

            response = web.StreamResponse(
                headers={"Content-Type": "video/x-flv", "Cache-Control": "no-store"}
            )
            await response.prepare(request)
            try:
                await response.write(first)
                async for chunk in stream:
                    await response.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                _LOGGER.debug("Relay client for %s disconnected", serial)
            except asyncio.TimeoutError:
                _LOGGER.debug("Zmodo live stream for %s stalled", serial)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Zmodo live relay for %s failed: %s", serial, err)
            return response


class ZmodoAlertMediaView(HomeAssistantView):
    """Proxy an alert's image or MP4 clip from the platform API."""

    url = "/api/zmodo/alert/{entry_id}/{alert_id}/{kind}"
    name = "api:zmodo:alert"
    requires_auth = False  # checked in _allowed()

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def head(self, request: web.Request, entry_id: str, alert_id: str, kind: str):
        return await self.get(request, entry_id, alert_id, kind)

    async def get(
        self, request: web.Request, entry_id: str, alert_id: str, kind: str
    ) -> web.StreamResponse:
        if not _allowed(self.hass, request):
            return web.Response(status=401)
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            return web.Response(status=404)
        try:
            path = alert_media_path(alert_id, kind)
        except ValueError:
            return web.Response(status=404)

        upstream_headers = {}
        if "Range" in request.headers:
            upstream_headers["Range"] = request.headers["Range"]

        response: web.StreamResponse | None = None
        try:
            async with coordinator.platform.open_media(path, upstream_headers) as upstream:
                if upstream.status >= 400:
                    return web.Response(status=upstream.status)
                response = web.StreamResponse(status=upstream.status)
                for name in _PASS_HEADERS:
                    if name in upstream.headers:
                        response.headers[name] = upstream.headers[name]
                response.headers["Cache-Control"] = "private, max-age=3600"
                await response.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            _LOGGER.debug("Alert media client for %s disconnected", alert_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zmodo alert %s %s failed: %s", alert_id, kind, err)
            if response is None:
                return web.Response(status=502)
        return response if response is not None else web.Response(status=502)
