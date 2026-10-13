"""End-to-end test for the direct-LAN (port 8000) stream.

Runnable standalone: ``python -I tests/test_local_stream.py`` (no Home Assistant
needed). Spins up a fake camera that speaks the port-8000 protocol and checks
that the client logs in, auto-fetches the AES key, requests HD, and that the
media frames parse and decrypt correctly through stream.parse_frame.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import struct
import sys
import types

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ZMODO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "zmodo",
)


def _load():
    """Load zmodo.local_stream and zmodo.stream without running the package __init__."""
    pkg = types.ModuleType("zt")
    pkg.__path__ = [ZMODO_DIR]
    sys.modules["zt"] = pkg
    mods = {}
    for name in ("local_stream", "stream"):
        spec = importlib.util.spec_from_file_location(
            f"zt.{name}", os.path.join(ZMODO_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"zt.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods["local_stream"], mods["stream"]


L, S = _load()

KEY = "0123456789ABCDEF0123456789ABCDEF"  # 32 ascii chars, used directly as AES-256 key


def _video_frame(tag: bytes, payload: bytes, hms=(16, 3, 6), ts=0) -> bytes:
    h, m, s = hms
    header = (
        tag + struct.pack("<I", len(payload)) + bytes([h, m, s]) + b"\x00" * 5
        + struct.pack("<Q", ts) + b"\x00" * 8
    )
    assert len(header) == L.VIDEO_HEADER_LEN
    return header + payload


def _encrypt_keyframe(plaintext: bytes) -> bytes:
    enc = Cipher(algorithms.AES(KEY.encode()), modes.CBC(bytes(16))).encryptor()
    head = enc.update(plaintext[:256]) + enc.finalize()
    return head + plaintext[256:]


# A keyframe payload with SPS/PPS/IDR NAL start codes so it's realistic; the
# plaintext is recovered after decrypt. Must be >= 256 bytes and a /16 length.
KEYFRAME_PLAIN = (b"\x00\x00\x01\x67" + b"\xAB" * 300)  # 304 bytes
PFRAME_PLAIN = b"\x00\x00\x01\x41" + b"\xCD" * 100


async def _fake_camera(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    async def read_frame():
        hdr = await reader.readexactly(L.CONTROL_HEADER_LEN)
        assert hdr[:4] == L._MAGIC_BYTES, "client sent bad magic"
        (length,) = struct.unpack_from("<I", hdr, 4)
        (opcode,) = struct.unpack_from("<H", hdr, 10)
        payload = await reader.readexactly(length) if length else b""
        return opcode, payload

    # 1) login
    op, payload = await read_frame()
    assert op == L.OP_LOGIN, op
    assert payload[:11] == b"ZMDNVRLOGIN" and payload[16:27] == b"Zmodo963852", payload
    writer.write(L.build_frame(L.OP_LOGIN, b"\x01" + b"\x00" * 7))
    # 2) get-key (client has no key, so it must ask)
    op, _ = await read_frame()
    assert op == L.OP_GET_KEY, op
    writer.write(L.build_frame(L.OP_GET_KEY, KEY.encode() + b"\x00\x00\x00\x00"))
    # 3) start video (HD)
    op, _ = await read_frame()
    assert op == L.OP_START_HD, op
    # ack control frame, then media: a control keepalive interleaved to exercise skip
    writer.write(L.build_frame(L.OP_START_HD, b"\x00\x00\x00\x00"))
    writer.write(_video_frame(L.TAG_KEYFRAME, _encrypt_keyframe(KEYFRAME_PLAIN), ts=0))
    writer.write(L.build_frame(0x9999, b"keepalive"))  # control frame mid-stream
    writer.write(_video_frame(L.TAG_PFRAME, PFRAME_PLAIN, ts=100_000))
    await writer.drain()
    await asyncio.sleep(0.2)
    writer.close()


async def main() -> None:
    server = await asyncio.start_server(_fake_camera, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    got_key = []
    frames = []
    gen = L.stream_local(
        "127.0.0.1", "", hd=True, port=port, on_key=got_key.append
    )
    try:
        async for msg in gen:
            frames.append(S.parse_frame(msg, got_key[0]))
            if len(frames) == 2:
                break
    finally:
        await gen.aclose()
    server.close()
    await server.wait_closed()

    # Key was auto-fetched over the session.
    assert got_key == [KEY], got_key
    # Keyframe first, decrypted back to the original plaintext.
    kf, pf = frames
    assert kf.is_keyframe and kf.tag == L.TAG_KEYFRAME
    assert kf.payload == KEYFRAME_PLAIN, "keyframe did not decrypt to plaintext"
    assert kf.camera_seconds == 16 * 3600 + 3 * 60 + 6
    # P-frame passed through in the clear; control frame was skipped.
    assert not pf.is_keyframe and pf.tag == L.TAG_PFRAME
    assert pf.payload == PFRAME_PLAIN

    # build_frame structure sanity.
    f = L.build_frame(L.OP_START_SD)
    assert f == struct.pack("<I", L._MAGIC) + b"\x00\x00\x00\x00\x00\x00\x02\x90"
    print("test_local_stream: OK")


if __name__ == "__main__":
    asyncio.run(main())
