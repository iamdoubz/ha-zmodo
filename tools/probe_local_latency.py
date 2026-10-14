#!/usr/bin/env python3
"""Measure the direct-LAN (port 8000) stream latency — no Home Assistant needed.

Connects straight to a camera, starts video, and reports how far behind the
camera's own clock each frame arrives — the same metric the integration exposes
as ``zmodo_delay_seconds``. Because it reads only the cleartext frame header, it
measures the *raw socket -> Python* lag, with none of the HA relay / go2rtc /
browser-buffering overhead. Compare this number to what you see in HA to find
out where the ~4 s is coming from.

    python tools/probe_local_latency.py 192.168.1.23
    python tools/probe_local_latency.py 192.168.1.23 --op 0x90a2 --seconds 20

Opcodes: 0x5000 HD mainstream (default), 0x90a2 SD substream, 0x9002 SD mainstream.

The delay is camera wall-clock vs your PC's clock, so it assumes the camera and
this PC are in the same time zone (they print both clocks so you can sanity-check)
and that the camera's clock is NTP-correct. Resolution is ~1 s (the header carries
only h:m:s), so watch the min / median over many frames, not a single frame.

Nothing is written to the camera. Dependencies: stdlib only.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

PORT = 8000
MAGIC = struct.pack("<I", 0xAAAA5555)
OP_LOGIN = 0x9009
USER = b"ZMDNVRLOGIN"
PASSWORD = b"Zmodo963852"
VIDEO_TAGS = (b"00dc", b"01dc")
TAG_KEYFRAME = b"00dc"


def frame(op: int, payload: bytes = b"") -> bytes:
    return MAGIC + struct.pack("<I", len(payload)) + b"\x00\x00" + struct.pack("<H", op) + payload


def field(value: bytes, size: int = 16) -> bytes:
    return value + b"\x00" * (size - len(value))


def local_seconds_now() -> float:
    """Seconds since local midnight, with sub-second precision."""
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec + (time.time() % 1.0)


def camera_delay(camera_seconds: int, local_seconds: float) -> float:
    """Signed seconds the local clock is ahead of the camera clock (midnight-safe)."""
    return (local_seconds - camera_seconds + 43200) % 86400 - 43200


class Conn:
    def __init__(self, ip: str):
        self.s = socket.create_connection((ip, PORT), timeout=6)
        self.s.settimeout(6)
        self.buf = b""

    def send(self, data: bytes) -> None:
        self.s.sendall(data)

    def _want(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("camera closed the connection")
            self.buf += chunk

    def next_message(self):
        self._want(4)
        tag = self.buf[:4]
        if tag == MAGIC:
            self._want(12)
            length = struct.unpack_from("<I", self.buf, 4)[0]
            op = struct.unpack_from("<H", self.buf, 10)[0]
            self._want(12 + length)
            payload = self.buf[12:12 + length]
            self.buf = self.buf[12 + length:]
            return "ctrl", op, payload
        if tag in VIDEO_TAGS:
            self._want(32)
            length = struct.unpack_from("<I", self.buf, 4)[0]
            h, m, sec = self.buf[8], self.buf[9], self.buf[10]
            self._want(32 + length)
            self.buf = self.buf[32 + length:]
            cam = h * 3600 + m * 60 + sec if h < 24 and m < 60 and sec < 60 else -1
            return "video", tag, cam
        raise ValueError(f"unexpected frame tag {tag!r} (is this a Zmodo port-8000 stream?)")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def login(c: Conn) -> bool:
    c.send(frame(OP_LOGIN, field(USER) + field(PASSWORD)))
    kind, op, _ = c.next_message()
    return kind == "ctrl" and op == OP_LOGIN


def selftest() -> int:
    assert round(camera_delay(100, 104.0), 1) == 4.0
    assert round(camera_delay(10, 14.5), 1) == 4.5
    # local just after midnight, camera just before: small positive, not ~ -86400
    assert round(camera_delay(86399, 2.0), 1) == 3.0
    print("selftest OK: camera_delay math (incl. midnight wrap)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure Zmodo port-8000 stream latency")
    ap.add_argument("ip", nargs="?", help="camera LAN IP")
    ap.add_argument("--op", default="0x5000",
                    help="start-video opcode (0x5000 HD, 0x90a2 SD substream, 0x9002 SD main)")
    ap.add_argument("--seconds", type=float, default=15.0, help="measurement duration")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.ip:
        ap.error("camera IP required (or --selftest)")
    op = int(a.op, 0)

    print(f"Connecting to {a.ip}:{PORT} ...")
    try:
        c = Conn(a.ip)
    except OSError as err:
        print(f"FAIL: cannot connect to {a.ip}:{PORT} ({err}).")
        return 1
    try:
        if not login(c):
            print("FAIL: login not acknowledged (built-in creds may differ on this model).")
            return 1
        print(f"OK:   login accepted. Measuring op 0x{op:04x} for {a.seconds:g}s "
              f"(first keyframe can lag; watch min/median)…\n")
        c.send(frame(op))

        delays: list[float] = []
        kf = pf = 0
        first = None
        deadline = time.time() + a.seconds
        next_print = 0.0
        while time.time() < deadline:
            try:
                kind, tag, cam = c.next_message()
            except (socket.timeout, ConnectionError) as err:
                print(f"  stream ended: {err}")
                break
            if kind != "video" or cam < 0:
                continue
            now_local = local_seconds_now()
            d = camera_delay(cam, now_local)
            if not (-60 < d < 600):   # implausible: wrong tz / bad clock
                continue
            delays.append(d)
            if tag == TAG_KEYFRAME:
                kf += 1
            else:
                pf += 1
            if first is None:
                first = (cam, now_local)
            if time.time() >= next_print:
                next_print = time.time() + 0.5
                recent = delays[-30:]
                print(f"  frames={len(delays):4d}  now={d:5.1f}s  "
                      f"min={min(delays):4.1f}s  avg30={sum(recent)/len(recent):4.1f}s  "
                      f"cam_clock={cam//3600:02d}:{cam%3600//60:02d}:{cam%60:02d}")
    finally:
        c.close()

    print("\n--- Summary ---")
    if not delays:
        print("No timestamped video frames captured (opcode unsupported, or header had no clock).")
        return 1
    delays_sorted = sorted(delays)
    median = delays_sorted[len(delays_sorted) // 2]
    fps = (kf + pf) / a.seconds
    print(f"frames: {len(delays)} ({kf} keyframe / {pf} p-frame), ~{fps:.1f} fps")
    print(f"latency (camera clock -> this PC): min {min(delays):.1f}s  "
          f"median {median:.1f}s  max {max(delays):.1f}s")
    print("\nThis is the raw LAN+decode lag. If HA shows notably more, the extra")
    print("is the relay -> go2rtc -> browser pipeline, not the camera link.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
