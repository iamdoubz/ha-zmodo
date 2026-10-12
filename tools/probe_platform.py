"""Probe the new user.zmodo.com API and sniff the live-stream format.

Usage:  pip install aiohttp
        python tools/probe_platform.py you@example.com
        (prompts for the password; nothing is written except stream_sample.bin)

Prints devices, recent alerts, opens a live session for the first online
camera, and dumps the first WebSocket frames so we can tell whether the
stream is FLV, fMP4, MPEG-TS, raw H.264 or a JSON control protocol.
"""
import asyncio
import getpass
import importlib.util
import pathlib
import sys

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "platform_api", ROOT / "custom_components" / "zmodo" / "platform_api.py"
)
platform_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_api)

FRAMES = 15
SAMPLE_BYTES = 2_000_000


def guess(b: bytes) -> str:
    if b[:3] == b"FLV":
        return "FLV"
    if b[4:8] in (b"ftyp", b"moof", b"moov", b"styp", b"mdat"):
        return f"fMP4 box '{b[4:8].decode()}'"
    if b[:1] == b"\x47" and (len(b) < 189 or b[188:189] == b"\x47"):
        return "MPEG-TS"
    if b[:4] in (b"\x00\x00\x00\x01",) or b[:3] == b"\x00\x00\x01":
        return "Annex-B H.264/H.265 NAL"
    if b[:1] in (b"{", b"["):
        return "JSON"
    return "unknown"


async def main(email: str) -> None:
    password = getpass.getpass("Zmodo password: ")
    async with aiohttp.ClientSession() as s:
        api = platform_api.ZmodoPlatformApi(s, email, password)
        await api.login()
        print("Login OK; access token expires", api._expires_at)

        devices = await api.get_devices()
        for d in devices:
            print(f"  device {d['name']!r:16} serial={d['serial']} online={d['online']}")

        alerts = await api.get_alerts(5)
        print(f"{len(alerts)} alerts; newest: {alerts[0] if alerts else None}")

        cam = next((d for d in devices if d.get("online")), None)
        if not cam:
            print("No online camera; skipping stream probe")
            return

        live = await api.start_live_session(cam["serial"])
        print("Live session:", {k: v for k, v in live.items() if k != "session"})

        out = open(ROOT / "stream_sample.bin", "wb")
        total = 0
        async with s.ws_connect(
            live["playlist"],
            headers={"Authorization": f"Bearer {api.access_token}",
                     "Origin": "https://user.zmodo.com"},
            heartbeat=20,
        ) as ws:
            print("WebSocket protocol:", ws.protocol)
            for i in range(FRAMES * 20):
                msg = await asyncio.wait_for(ws.receive(), timeout=15)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if i < FRAMES:
                        print(f"[{i}] TEXT {msg.data[:300]}")
                    continue
                if msg.type != aiohttp.WSMsgType.BINARY:
                    print(f"[{i}] {msg.type} {msg.data}")
                    break
                b = msg.data
                if i < FRAMES:
                    print(f"[{i}] BINARY len={len(b):6} {guess(b):24} {b[:32].hex(' ')}")
                if total < SAMPLE_BYTES:
                    out.write(b)
                    total += len(b)
            out.close()
        print(f"Wrote {total} bytes to stream_sample.bin")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else input("Email: ")))
