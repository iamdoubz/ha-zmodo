"""Decode the user.zmodo.com live WebSocket stream and remux it to FLV.

Wire format (reverse-engineered Oct 2026):

* First message is TEXT JSON: {"type":"config","aesKey":"<32 hex chars>","audio":bool}
* Every following message is BINARY: a 32-byte header + payload.

  Header (little-endian):
    [0:4]   fourcc   b"00dc" = video keyframe, b"01dc" = video P-frame
                     (audio, if enabled, presumably b"01wb" — not yet seen)
    [4:8]   uint32   payload length
    [8:11]  h, m, s  camera wall-clock time
    [16:24] uint64   capture timestamp in microseconds
    [24]    flags

  Payload: H.264 Annex-B (640x480 Main profile on the SD stream).

* Keyframes are encrypted: only the first 256 bytes, AES-256-CBC with a zero IV,
  using the ASCII bytes of the aesKey hex string as the 32-byte key.
  P-frames are sent in the clear.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

HEADER_LEN = 32
WS_IDLE_TIMEOUT = 20          # seconds without a frame before giving up
WS_MAX_MSG_SIZE = 16 * 1024 * 1024
PLATFORM_ORIGIN = "https://user.zmodo.com"
ENCRYPTED_PREFIX = 256
TAG_KEYFRAME = b"00dc"
TAG_PFRAME = b"01dc"


@dataclass
class ZmodoFrame:
    """One parsed media frame."""

    tag: bytes
    timestamp_us: int
    payload: bytes

    @property
    def is_video(self) -> bool:
        return self.tag.endswith(b"dc")

    @property
    def is_keyframe(self) -> bool:
        return self.tag == TAG_KEYFRAME


def parse_config(text: str) -> dict:
    """Parse the leading TEXT config message."""
    data = json.loads(text)
    if data.get("type") != "config":
        raise ValueError(f"Unexpected first message: {text[:200]}")
    return data


def decrypt_keyframe(payload: bytes, aes_key: str) -> bytes:
    """Decrypt the encrypted prefix of a keyframe payload."""
    n = min(ENCRYPTED_PREFIX, len(payload) // 16 * 16)
    if n == 0:
        return payload
    dec = Cipher(algorithms.AES(aes_key.encode("ascii")), modes.CBC(bytes(16))).decryptor()
    return dec.update(payload[:n]) + dec.finalize() + payload[n:]


def parse_frame(message: bytes, aes_key: str) -> ZmodoFrame:
    """Parse (and decrypt if needed) one binary WebSocket message."""
    if len(message) < HEADER_LEN:
        raise ValueError(f"Short frame ({len(message)} bytes)")
    tag = message[:4]
    (length,) = struct.unpack_from("<I", message, 4)
    (ts_us,) = struct.unpack_from("<Q", message, 16)
    payload = message[HEADER_LEN:HEADER_LEN + length]
    if tag == TAG_KEYFRAME:
        payload = decrypt_keyframe(payload, aes_key)
    return ZmodoFrame(tag, ts_us, payload)


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (start codes removed)."""
    nals: list[bytes] = []
    i, n, start = 0, len(data), -1
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start >= 0:
                end = i - 1 if i > 0 and data[i - 1] == 0 else i
                nals.append(data[start:end])
            i += 3
            start = i
        else:
            i += 1
    if start >= 0 and start < n:
        nals.append(data[start:])
    return [nal for nal in nals if nal]


class FlvMuxer:
    """Minimal video-only FLV muxer for H.264 Annex-B access units."""

    def __init__(self) -> None:
        self._sent_header = False
        self._sent_config = False
        self._t0: int | None = None

    @staticmethod
    def _tag(tag_type: int, ts_ms: int, body: bytes) -> bytes:
        ts_ms &= 0xFFFFFFFF
        head = struct.pack(
            ">B3sBBBB3s",
            tag_type,
            len(body).to_bytes(3, "big"),
            (ts_ms >> 16) & 0xFF, (ts_ms >> 8) & 0xFF, ts_ms & 0xFF,
            (ts_ms >> 24) & 0xFF,
            b"\0\0\0",
        )
        return head + body + struct.pack(">I", 11 + len(body))

    def mux(self, frame: ZmodoFrame) -> bytes:
        """Return FLV bytes for one frame (may be empty until the first keyframe)."""
        if not frame.is_video:
            return b""
        nals = split_annexb(frame.payload)
        out = b""
        if not self._sent_header:
            out += b"FLV\x01\x01\x00\x00\x00\x09" + b"\0\0\0\0"
            self._sent_header = True

        if not self._sent_config:
            sps = next((x for x in nals if x[0] & 0x1F == 7), None)
            pps = next((x for x in nals if x[0] & 0x1F == 8), None)
            if not (frame.is_keyframe and sps and pps):
                return out  # wait for a decodable keyframe
            self._t0 = frame.timestamp_us
            record = (
                bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
                + struct.pack(">H", len(sps)) + sps
                + b"\x01" + struct.pack(">H", len(pps)) + pps
            )
            out += self._tag(9, 0, b"\x17\x00\x00\x00\x00" + record)
            self._sent_config = True

        assert self._t0 is not None
        ts_ms = max(0, (frame.timestamp_us - self._t0) // 1000)
        body = b"".join(
            struct.pack(">I", len(x)) + x
            for x in nals
            if x[0] & 0x1F not in (7, 8, 9)  # SPS/PPS live in the config record; drop AUDs
        )
        if not body:
            return out
        kind = 0x17 if frame.is_keyframe else 0x27
        out += self._tag(9, ts_ms, bytes([kind, 0x01, 0, 0, 0]) + body)
        return out


async def iter_live_flv(
    session: aiohttp.ClientSession,
    platform: Any,
    serial: str,
    fallback_aes_key: str = "",
) -> AsyncIterator[bytes]:
    """Open a live session for *serial* and yield FLV bytes until it ends.

    *platform* is a ZmodoPlatformApi (duck-typed to avoid an import cycle).
    """
    live = await platform.start_live_session(serial)
    url = live.get("playlist")
    if not url:
        raise ValueError(f"Live session response has no playlist: {live}")

    token = await platform.ensure_token()
    muxer = FlvMuxer()
    aes_key = fallback_aes_key
    async with session.ws_connect(
        url,
        headers={"Authorization": f"Bearer {token}", "Origin": PLATFORM_ORIGIN},
        heartbeat=20,
        max_msg_size=WS_MAX_MSG_SIZE,
    ) as ws:
        _LOGGER.debug("Zmodo live stream opened for %s", serial)
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=WS_IDLE_TIMEOUT)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    cfg = parse_config(msg.data)
                except ValueError:
                    _LOGGER.debug("Ignoring text message: %s", msg.data[:200])
                    continue
                aes_key = cfg.get("aesKey") or aes_key
                continue
            if msg.type != aiohttp.WSMsgType.BINARY:
                _LOGGER.debug("Zmodo live stream for %s ended: %s", serial, msg.type)
                return
            chunk = muxer.mux(parse_frame(msg.data, aes_key))
            if chunk:
                yield chunk
