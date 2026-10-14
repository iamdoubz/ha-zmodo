"""Cloud PTZ control for Zmodo cameras.

Reverse-engineered from the Android app (com.zmodo 8.1.0, native ``libcore.so``
``zmdnet`` SDK) and proven end-to-end against a real camera. PTZ has no REST
endpoint and is not on the local port-8000 protocol; it rides a cloud WebSocket
command channel:

    wss://<access_server.user_conn host>:8446/ws    (TLS, cleartext JSON inside)

    1. Login  (type:1) with the streaming token  -> {"type":1,"code":200,...}
    2. PTZ    (type:3): cmd=0x1000009, ptz_cmd in {0..4}, from_id = streaming JWT
                        -> device acks cmd=0x1100009

Everything needed comes from the platform login's ``access_server`` block, which
the integration already receives. Buttons send a *timed nudge*: move for ``hold``
seconds, then stop (ptz_cmd 0), which matches HA's momentary-press button model.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

WS_SCHEME = "wss"                   # overridable in tests to hit a plain ws:// fake
WS_PORT = 8446
WS_PATH = "ws"
PTZ_CONFIG_CMD = 0x1000009          # "cmd" field for a PTZ-config message
CLIENT_TYPE = 1                     # Login client_type (app constant)
CLIENT_VERSION = "8.1.0"            # Login client_version (app constant)
DEFAULT_HOLD = 0.6                  # seconds a button press pans before auto-stop

# on-screen direction -> ptz_cmd int (from the app's PtzControls button defs)
PTZ_DIRECTIONS = {"left": 1, "right": 2, "up": 3, "down": 4, "stop": 0}


def supports_ptz(device: dict[str, Any]) -> bool:
    """Best-effort: does this camera have motorised pan/tilt?

    Shows the buttons by default — hiding a genuinely PTZ camera is worse than
    the reverse, and the SD-H2001 test cameras expose no obvious flag. When
    ``device_capacities`` is present but has no pan/tilt marker we recognise, the
    payload is logged so the gate can be tightened once a confirmed non-PTZ
    device's capacities is known.
    # ponytail: make this strict (return the token match) when that sample exists.
    """
    cap = device.get("device_capacities")
    if cap in (None, "", [], {}):
        return True
    text = cap.lower() if isinstance(cap, str) else json.dumps(cap).lower()
    if any(k in text for k in ("ptz", "pan", "tilt", '"pt"')):
        return True
    _LOGGER.debug(
        "PTZ capability unknown for %s; device_capacities=%r — showing buttons anyway",
        device.get("serial") or device.get("physical_id"), cap,
    )
    return True


class ZmodoPtz:
    """Opens a short-lived cloud WebSocket per nudge and pans the camera."""

    def __init__(self, platform) -> None:
        self._platform = platform
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, serial: str) -> asyncio.Lock:
        # one in-flight nudge per camera; serialises press/stop pairs
        return self._locks.setdefault(serial, asyncio.Lock())

    async def nudge(self, serial: str, direction: str, hold: float = DEFAULT_HOLD) -> None:
        """Pan ``direction`` for ``hold`` seconds, then stop."""
        cmd = PTZ_DIRECTIONS.get(direction)
        if cmd is None:
            raise ValueError(f"unknown PTZ direction {direction!r}")
        async with self._lock(serial):
            await self._run(serial, cmd, hold)

    async def _run(self, serial: str, cmd: int, hold: float) -> None:
        # ensure_token() logs in when needed, which (re)populates access_server
        await self._platform.ensure_token()
        acc = self._platform.access_server or {}
        jwt = acc.get("token_id")
        client_id = acc.get("client_id", "")
        cid = str(acc.get("cid", "0"))
        hosts = [str(h).split(":")[0] for h in (acc.get("user_conn") or [])]
        if acc.get("acc_srv_ip"):
            hosts.append(str(acc["acc_srv_ip"]).split(":")[0])
        if not (jwt and hosts):
            raise RuntimeError("no streaming session (access_server) available for PTZ")

        login = {
            "type": 1, "length": 0, "cmd_version": "0.0.1", "data_type": 0,
            "token_id": jwt, "client_type": CLIENT_TYPE,
            "client_version": CLIENT_VERSION, "client_id": client_id, "cid": cid,
        }
        mid = 0

        def ptz(ptz_cmd: int) -> str:
            nonlocal mid
            mid += 1
            return json.dumps({
                "type": 3, "length": 0, "message_id": mid, "cmd": PTZ_CONFIG_CMD,
                "from_id": jwt, "to_id": serial, "channel": 0,
                "ptz_cmd": ptz_cmd, "para0": 0, "para1": 0, "uid": serial,
            })

        session = self._platform.session
        last_err: Exception | None = None
        for host in hosts:  # try nodes in order; first that logs in wins
            url = f"{WS_SCHEME}://{host}:{WS_PORT}/{WS_PATH}"
            try:
                # ssl=False: the appconn cert is pinned / IP-mismatched; payload
                # auth is the JWT in the Login frame, not the TLS cert.
                async with session.ws_connect(url, ssl=False, heartbeat=None) as ws:
                    await ws.send_str(json.dumps(login))
                    ack = await ws.receive(timeout=5)
                    if ack.type is aiohttp.WSMsgType.TEXT:
                        code = (json.loads(ack.data) or {}).get("code")
                        if code and code != 200:
                            raise RuntimeError(f"PTZ login refused (code {code})")
                    elif ack.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError(f"PTZ login socket closed ({ack.type})")
                    await ws.send_str(ptz(cmd))
                    if cmd != 0 and hold > 0:
                        await asyncio.sleep(hold)
                        await ws.send_str(ptz(0))
                    _LOGGER.debug("PTZ %s cmd=%s via %s ok", serial, cmd, host)
                    return
            except Exception as err:  # noqa: BLE001
                last_err = err
                _LOGGER.debug("PTZ via %s failed: %s", host, err)
        raise RuntimeError(f"PTZ failed on all nodes: {last_err}")
