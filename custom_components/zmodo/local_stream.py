"""Direct LAN live stream for Zmodo cameras (port 8000), bypassing the cloud.

The camera exposes a proprietary binary TCP protocol (``ZMDNETLIB``) on port
8000. Reverse-engineered from the camera firmware (``AppAmba33``); see the
project brief. It is the same media format the cloud WebSocket carries, so the
payloads flow straight into :func:`.stream.parse_frame`.

Wire protocol (all little-endian):

* Every message has a 12-byte header: magic ``0xAAAA5555`` (4), payload length
  (4), unused/seq (1), channel (1), opcode (2), then ``length`` payload bytes.
* ``0x9009`` login: payload ``username[16] + password[16]`` (null-padded). The
  firmware ships fixed credentials that always authenticate.
* ``0x9636`` get-key: the camera returns its per-device AES key (ASCII) — the
  same key the cloud ``/device`` API reports, used to decrypt keyframes.
* ``0x9002`` / ``0x5000`` start video (SD 640x480 / HD 1920x1080): header-only.
  The camera replies with one control frame, then streams raw media frames with
  the ``00dc``/``01dc`` 32-byte-header format, identical to the cloud stream.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import AsyncIterator, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

LOCAL_STREAM_PORT = 8000

_MAGIC = 0xAAAA5555
_MAGIC_BYTES = struct.pack("<I", _MAGIC)

OP_LOGIN = 0x9009
OP_GET_KEY = 0x9636
OP_START_SD = 0x9002       # 640x480 mainstream
OP_START_SD_SUB = 0x90a2   # 640x480 substream (runs concurrently with HD mainstream)
OP_START_HD = 0x5000       # 1920x1080 mainstream

# Firmware-embedded credentials (identical on every unit of this generation).
_USER = b"ZMDNVRLOGIN"
_PASSWORD = b"Zmodo963852"

TAG_KEYFRAME = b"00dc"
TAG_PFRAME = b"01dc"
_VIDEO_TAGS = (TAG_KEYFRAME, TAG_PFRAME)

VIDEO_HEADER_LEN = 32
CONTROL_HEADER_LEN = 12
MAX_PAYLOAD = 8 * 1024 * 1024   # sanity cap; a 1080p keyframe is well under 1 MB

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 20.0


class LocalProtocolError(Exception):
    """The camera sent something that doesn't fit the port-8000 protocol."""


def _cred_field(value: bytes, size: int = 16) -> bytes:
    if len(value) >= size:
        raise ValueError("credential too long")
    return value + b"\x00" * (size - len(value))


def build_frame(opcode: int, payload: bytes = b"", channel: int = 0) -> bytes:
    """Build one protocol message (12-byte header + payload)."""
    return (
        _MAGIC_BYTES
        + struct.pack("<I", len(payload))
        + bytes([0, channel])
        + struct.pack("<H", opcode)
        + payload
    )


async def _read_exact(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(n), timeout)
    except asyncio.IncompleteReadError as err:
        raise ConnectionError("camera closed the connection") from err


async def _read_control_frame(
    reader: asyncio.StreamReader, timeout: float
) -> tuple[int, bytes]:
    """Read one full ``0xAAAA5555`` control/response frame; return (opcode, payload)."""
    header = await _read_exact(reader, CONTROL_HEADER_LEN, timeout)
    if header[:4] != _MAGIC_BYTES:
        raise LocalProtocolError(f"bad magic {header[:4].hex()}")
    (length,) = struct.unpack_from("<I", header, 4)
    (opcode,) = struct.unpack_from("<H", header, 10)
    if length > MAX_PAYLOAD:
        raise LocalProtocolError(f"control payload too large ({length})")
    payload = await _read_exact(reader, length, timeout) if length else b""
    return opcode, payload


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    aes_key: str,
    stream_op: int,
    timeout: float,
) -> str:
    """Log in, fetch the AES key if needed, and request the live stream.

    Returns the AES key to decrypt keyframes with.
    """
    writer.write(build_frame(OP_LOGIN, _cred_field(_USER) + _cred_field(_PASSWORD)))
    await writer.drain()
    opcode, _ = await _read_control_frame(reader, timeout)
    if opcode != OP_LOGIN:
        raise LocalProtocolError(f"login not acknowledged (opcode {opcode:#06x})")

    if not aes_key:
        writer.write(build_frame(OP_GET_KEY, b"\x00\x00\x00\x00"))
        await writer.drain()
        opcode, payload = await _read_control_frame(reader, timeout)
        key = payload.split(b"\x00", 1)[0].decode("ascii", "ignore")
        if opcode != OP_GET_KEY or len(key) != 32:
            raise LocalProtocolError("camera did not return a usable AES key")
        aes_key = key

    writer.write(build_frame(stream_op))
    await writer.drain()
    return aes_key


async def _read_video_message(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Return one raw media frame (``00dc``/``01dc`` 32-byte header + payload).

    Control frames (the start-video ack, keepalives) are skipped transparently.
    """
    while True:
        tag = await _read_exact(reader, 4, timeout)
        if tag == _MAGIC_BYTES:  # control frame: read and discard its body
            rest = await _read_exact(reader, CONTROL_HEADER_LEN - 4, timeout)
            (length,) = struct.unpack_from("<I", rest, 0)
            if length > MAX_PAYLOAD:
                raise LocalProtocolError(f"control payload too large ({length})")
            if length:
                await _read_exact(reader, length, timeout)
            continue
        if tag in _VIDEO_TAGS:
            header = tag + await _read_exact(reader, VIDEO_HEADER_LEN - 4, timeout)
            (length,) = struct.unpack_from("<I", header, 4)
            if length > MAX_PAYLOAD:
                raise LocalProtocolError(f"video payload too large ({length})")
            return header + await _read_exact(reader, length, timeout)
        raise LocalProtocolError(f"unexpected frame tag {tag!r}")


async def stream_local(
    host: str,
    aes_key: str = "",
    *,
    stream_op: int = OP_START_HD,
    port: int = LOCAL_STREAM_PORT,
    open_connection: Callable[..., Any] = asyncio.open_connection,
    connect_timeout: float = CONNECT_TIMEOUT,
    read_timeout: float = READ_TIMEOUT,
    on_key: Callable[[str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Connect, authenticate and yield raw media frames from a camera on the LAN.

    ``stream_op`` selects the stream: :data:`OP_START_HD` (1080p mainstream),
    :data:`OP_START_SD_SUB` (480p substream) or :data:`OP_START_SD`. HD and the
    substream can run on two concurrent connections. Each yielded value is a
    complete ``00dc``/``01dc`` message ready for :func:`.stream.parse_frame`.
    ``on_key`` (if given) is called with the AES key once the handshake resolves
    it, so the caller can cache/decrypt.
    """
    reader, writer = await asyncio.wait_for(
        open_connection(host, port), connect_timeout
    )
    try:
        key = await _handshake(reader, writer, aes_key, stream_op, read_timeout)
        if on_key is not None:
            on_key(key)
        _LOGGER.debug("Local Zmodo stream open to %s:%d (op %#06x)", host, port, stream_op)
        while True:
            yield await _read_video_message(reader, read_timeout)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), connect_timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
