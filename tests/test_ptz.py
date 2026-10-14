"""Standalone test for cloud PTZ (zmodo.ptz).

Runnable without Home Assistant:  ``python -I tests/test_ptz.py``

Spins up a fake cloud WebSocket that speaks the handshake the real appconn
server does (Login type:1 -> {"code":200}, then acks PTZ type:3 frames) and
checks that ZmodoPtz.nudge logs in and sends the right move+stop frames.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types

import aiohttp
from aiohttp import web

ZMODO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "zmodo",
)


def _load_ptz():
    """Load zmodo.ptz in isolation (it only needs aiohttp + stdlib)."""
    pkg = types.ModuleType("zt"); pkg.__path__ = [ZMODO_DIR]
    sys.modules["zt"] = pkg
    spec = importlib.util.spec_from_file_location("zt.ptz", os.path.join(ZMODO_DIR, "ptz.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["zt.ptz"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakePlatform:
    """Minimal stand-in for ZmodoPlatformApi."""

    def __init__(self, session, access_server):
        self.session = session
        self.access_server = access_server
        self.login_calls = 0

    async def ensure_token(self):
        self.login_calls += 1
        return "tok"


def _check_supports_ptz(ptz) -> None:
    sp = ptz.supports_ptz
    assert sp({}) is False, "default-deny when no capability info"
    assert sp({"platform_device_type": 1}) is False, "device_type 1 is never PTZ"
    assert sp({"platform_device_type": 2}) is True, "device_type 2 is PTZ"
    assert sp({"platform_device_type": 7}) is True
    assert sp({"platform_device_type": 14}) is True
    assert sp({"platform_device_type": 99}) is False, "unknown type, no 2a token"
    assert sp({"platform_device_type": 99, "device_capacities": "x:2a:y"}) is True
    assert sp({"platform_device_type": 1, "device_capacities": "2a"}) is False, "type 1 overrides"
    print("supports_ptz OK: device_type {2,7,14} or '2a' token; type 1 denied")


async def _main():
    ptz = _load_ptz()
    _check_supports_ptz(ptz)
    received: list[dict] = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            obj = json.loads(msg.data)
            received.append(obj)
            if obj.get("type") == 1:               # Login -> ack 200
                await ws.send_str(json.dumps({"type": 1, "length": 0, "code": 200,
                                              "heartbeat": 60, "timestamp": 0}))
            elif obj.get("type") == 3:             # PTZ -> device ack (resp bit set)
                await ws.send_str(json.dumps({"type": 3, "message_id": obj["message_id"],
                                              "cmd": obj["cmd"] | 0x100000}))
        return ws

    app = web.Application()
    app.router.add_get(f"/{ptz.WS_PATH}", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    # point ptz at the local fake over plain ws
    ptz.WS_SCHEME = "ws"
    ptz.WS_PORT = port

    async with aiohttp.ClientSession() as session:
        platform = _FakePlatform(session, {
            "token_id": "JWT.header.sig",
            "client_id": "uuid-1234",
            "cid": "0",
            "user_conn": ["127.0.0.1:6202"],
        })
        client = ptz.ZmodoPtz(platform)
        await client.nudge("ZMD13BLCD000151", "up", hold=0.05)

    await runner.cleanup()

    # --- assertions ---
    assert platform.login_calls == 1, "should ensure token once"
    types_seen = [m["type"] for m in received]
    assert types_seen == [1, 3, 3], f"expected Login, move, stop; got {types_seen}"

    login, move, stop = received
    assert login["token_id"] == "JWT.header.sig"
    assert login["client_id"] == "uuid-1234" and login["cid"] == "0"

    assert move["cmd"] == ptz.PTZ_CONFIG_CMD == 0x1000009
    assert move["ptz_cmd"] == 3, "up == 3"
    assert move["from_id"] == "JWT.header.sig", "from_id must be the streaming JWT"
    assert move["to_id"] == "ZMD13BLCD000151" and move["uid"] == "ZMD13BLCD000151"

    assert stop["ptz_cmd"] == 0, "release sends stop (0)"
    assert stop["message_id"] == move["message_id"] + 1

    print("test_ptz OK: login(code200) -> move(ptz_cmd=3,from_id=JWT) -> stop(0)")


if __name__ == "__main__":
    asyncio.run(_main())
