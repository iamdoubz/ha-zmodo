#!/usr/bin/env python3
"""Probe a Zmodo camera's direct-LAN (port 8000) stream — no Home Assistant needed.

Run this against a camera on your LAN to find out whether the local-stream path
this integration uses already works on *your* model, and to generate the
evidence to attach to a pull request adding support for it.

    python tools/probe_local_stream.py 192.168.1.23

It logs in with the firmware's built-in credentials, asks the camera for its own
AES key, then tries each known "start video" opcode. For every opcode that
streams, it saves a short raw H.264 sample (``<ip>_<opcode>.h264``) and, if
``ffprobe`` is on PATH, prints the resolution/codec. Nothing is written to the
camera; only reads and a video request are sent.

Dependencies: ``cryptography`` (already an integration dependency). ``ffprobe``
(from ffmpeg) is optional but recommended so the report includes resolutions.

See docs/PORTING_LOCAL_STREAM.md for the full porting workflow.
"""
from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import sys
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 8000
MAGIC = struct.pack("<I", 0xAAAA5555)

OP_LOGIN = 0x9009
OP_GET_KEY = 0x9636
# (opcode, label) pairs to try. 0x90a2 is the SD substream (runs concurrently
# with HD); 0x9002 is the SD mainstream; 0x5000 is the HD mainstream.
START_OPS = [(0x5000, "HD_mainstream"), (0x90a2, "SD_substream"), (0x9002, "SD_mainstream")]

# Firmware-embedded credentials. Identical across the current camera generation
# (Ambarella S2L). If your model rejects login, these may differ — see the doc.
USER = b"ZMDNVRLOGIN"
PASSWORD = b"Zmodo963852"

VIDEO_TAGS = (b"00dc", b"01dc")


def frame(op: int, payload: bytes = b"") -> bytes:
    return MAGIC + struct.pack("<I", len(payload)) + b"\x00\x00" + struct.pack("<H", op) + payload


def field(value: bytes, size: int = 16) -> bytes:
    return value + b"\x00" * (size - len(value))


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
        """Return ('ctrl', opcode, payload) or ('video', tag, payload)."""
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
            self._want(32 + length)
            payload = self.buf[32:32 + length]
            self.buf = self.buf[32 + length:]
            return "video", tag, payload
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


def get_key(c: Conn) -> str | None:
    c.send(frame(OP_GET_KEY, b"\x00\x00\x00\x00"))
    kind, op, payload = c.next_message()
    if kind != "ctrl" or op != OP_GET_KEY:
        return None
    for i in range(len(payload) - 31):
        chunk = payload[i:i + 32]
        if all(ch in b"0123456789ABCDEFabcdef" for ch in chunk):
            return chunk.decode("ascii")
    return None


def capture(c: Conn, op: int, key: bytes, out_path: str, seconds: float = 3.0) -> tuple[int, int]:
    """Request `op`, save decoded elementary stream, return (keyframes, pframes)."""
    c.send(frame(op))  # header-only start-video
    kf = pf = 0
    deadline = time.time() + seconds
    with open(out_path, "wb") as fh:
        while time.time() < deadline:
            try:
                kind, tag, payload = c.next_message()
            except (socket.timeout, ConnectionError):
                break
            if kind != "video":
                continue
            if tag == b"00dc":
                kf += 1
                if len(payload) >= 256:
                    dec = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
                    payload = dec.update(payload[:256]) + dec.finalize() + payload[256:]
            else:
                pf += 1
            fh.write(payload)
    return kf, pf


def ffprobe(path: str) -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        return "  (install ffmpeg and run: ffprobe " + path + ")"
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,profile,width,height,pix_fmt",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return "\n".join("  " + line for line in out.splitlines()) or "  (ffprobe found no video)"
    except Exception as err:  # noqa: BLE001
        return f"  (ffprobe failed: {err})"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    ip = sys.argv[1]
    print(f"Connecting to {ip}:{PORT} ...")
    try:
        c = Conn(ip)
    except OSError as err:
        print(f"FAIL: cannot connect to {ip}:{PORT} ({err}).")
        print("  Port 8000 may be closed, or the camera is off-LAN / isolated by the AP.")
        return 1

    try:
        if not login(c):
            print("FAIL: login was not acknowledged.")
            print("  The built-in credentials may differ on your model — see the doc (step 5).")
            return 1
        print(f"OK:   login accepted (user {USER.decode()}).")

        key = get_key(c)
        if not key:
            print("WARN: camera did not return a 32-char AES key via 0x9636.")
            print("  Keyframes can't be decrypted; note this in your PR. Continuing anyway.")
            key = "0" * 32
        else:
            print(f"OK:   camera served AES key ({key[:8]}... , {len(key)} chars).")
    finally:
        c.close()

    print("\nTrying start-video opcodes (fresh connection each):")
    results = []
    for op, label in START_OPS:
        try:
            c = Conn(ip)
            if not login(c):
                raise RuntimeError("login failed on retry")
            out = f"{ip}_{op:#06x}_{label}.h264"
            kf, pf = capture(c, op, key.encode(), out)
            c.close()
        except Exception as err:  # noqa: BLE001
            print(f"  0x{op:04x} {label:<14} ERROR: {err}")
            continue
        if kf + pf == 0:
            print(f"  0x{op:04x} {label:<14} no video (opcode not supported on this model)")
            continue
        print(f"  0x{op:04x} {label:<14} {kf} keyframes, {pf} p-frames -> {out}")
        print(ffprobe(out))
        results.append((op, label, out))

    print("\n--- Summary ---")
    if results:
        print("Working start opcodes:", ", ".join(f"0x{op:04x} ({lbl})" for op, lbl, _ in results))
        print("This model likely works with the existing integration. Attach the ffprobe")
        print("output above and your product_id to a PR - see docs/PORTING_LOCAL_STREAM.md.")
        return 0
    print("No opcode produced video. Login/key may still work; the start-video opcode")
    print("likely differs on your model. Reverse-engineer it with Ghidra — see the doc.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
