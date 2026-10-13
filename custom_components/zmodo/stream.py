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
WS_CLOSE_TIMEOUT = 2.0        # don't wait long for the server's close handshake

try:  # aiohttp >= 3.10
    _WS_TIMEOUT: dict[str, Any] = {
        "timeout": aiohttp.ClientWSTimeout(ws_receive=None, ws_close=WS_CLOSE_TIMEOUT)
    }
except AttributeError:  # pragma: no cover - older aiohttp
    _WS_TIMEOUT = {"timeout": WS_CLOSE_TIMEOUT}
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


FLV_FILE_HEADER = b"FLV\x01\x01\x00\x00\x00\x09" + b"\0\0\0\0"  # video only


def _flv_tag(tag_type: int, ts_ms: int, body: bytes) -> bytes:
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


class FlvMuxer:
    """Video-only FLV muxer for H.264 Annex-B access units.

    Timestamps stay monotonic across upstream reconnects: call
    ``new_source()`` before feeding frames from a new session.
    """

    def __init__(self) -> None:
        self.config_tag: bytes | None = None
        self._config_body: bytes | None = None
        self._t0: int | None = None
        self._base_ms = 0
        self._last_ms = -1
        self._need_config = True

    def new_source(self) -> None:
        """Prepare for frames from a new upstream session."""
        self._t0 = None
        self._base_ms = self._last_ms + 100 if self._last_ms >= 0 else 0
        self._need_config = True

    def mux_tags(self, frame: ZmodoFrame) -> list[tuple[bytes, bool]]:
        """Return [(tag_bytes, is_keyframe_or_config)] for one frame.

        Nothing is emitted until the first keyframe carrying SPS/PPS; a
        sequence-header (config) tag is emitted before the first keyframe of
        every source.
        """
        if not frame.is_video:
            return []
        nals = split_annexb(frame.payload)
        out: list[tuple[bytes, bool]] = []

        if self._t0 is None:
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
            self._config_body = b"\x17\x00\x00\x00\x00" + record

        ts_ms = self._base_ms + max(0, (frame.timestamp_us - self._t0) // 1000)
        ts_ms = max(ts_ms, self._last_ms + 1)  # strictly increasing DTS

        if self._need_config and self._config_body:
            self.config_tag = _flv_tag(9, ts_ms, self._config_body)
            out.append((self.config_tag, True))
            self._need_config = False

        body = b"".join(
            struct.pack(">I", len(x)) + x
            for x in nals
            if x[0] & 0x1F not in (7, 8, 9)  # SPS/PPS live in the config record; drop AUDs
        )
        if not body:
            return out
        kind = 0x17 if frame.is_keyframe else 0x27
        out.append((_flv_tag(9, ts_ms, bytes([kind, 0x01, 0, 0, 0]) + body), frame.is_keyframe))
        self._last_ms = ts_ms
        return out

    def mux(self, frame: ZmodoFrame) -> bytes:
        """Single-consumer helper: FLV file header + tags as one byte string."""
        out = b"".join(tag for tag, _ in self.mux_tags(frame))
        if out and not getattr(self, "_sent_header", False):
            self._sent_header = True
            out = FLV_FILE_HEADER + out
        return out


# ----------------------------------------------------------------------
# Shared live hub: one upstream Zmodo session per camera, many clients
# ----------------------------------------------------------------------

SUB_QUEUE_MAX = 60          # ~6 s at 10 fps; beyond this a client skips to the next keyframe
GOP_CACHE_MAX = SUB_QUEUE_MAX - 2  # frames kept since the last keyframe for instant joins
LAG_RESTART_SECONDS = 10.0  # frames arriving this far behind the camera clock -> new session
IDLE_GRACE_SECONDS = 5.0    # keep the upstream session briefly after the last client leaves
RETRY_DELAY_SECONDS = 3.0


class _Subscriber:
    __slots__ = ("queue", "skip_to_key")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        # True until the client is positioned on config + keyframe
        self.skip_to_key = True


class LiveHub:
    """Fan one camera's live session out to any number of FLV clients."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        platform: Any,
        serial: str,
        aes_key: str = "",
        create_task=None,
    ) -> None:
        self._http = session
        self._platform = platform
        self.serial = serial
        self.aes_key = aes_key
        self._create_task = create_task or asyncio.get_running_loop().create_task
        self._subs: set[_Subscriber] = set()
        self._muxer = FlvMuxer()
        self._gop: list[bytes] = []
        self._task: asyncio.Task | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self.sessions_opened = 0
        self.lag_restarts = 0

    # -- client side ---------------------------------------------------

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield FLV bytes for one client, starting at the latest keyframe."""
        sub = _Subscriber()
        if self._muxer.config_tag and self._gop:
            # Join mid-stream: config + cached GOP (starts with a keyframe),
            # queued synchronously so ordering with live tags is preserved.
            sub.queue.put_nowait(self._muxer.config_tag)
            for tag in self._gop:
                sub.queue.put_nowait(tag)
            sub.skip_to_key = False
        self._subs.add(sub)
        self._cancel_stop()
        self._ensure_running()
        try:
            yield FLV_FILE_HEADER
            while True:
                item = await asyncio.wait_for(sub.queue.get(), timeout=WS_IDLE_TIMEOUT)
                if item is None:
                    return
                yield item
        finally:
            self._subs.discard(sub)
            if not self._subs:
                self._schedule_stop()

    @property
    def client_count(self) -> int:
        return len(self._subs)

    # -- lifecycle -----------------------------------------------------

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._create_task(self._run())

    def _cancel_stop(self) -> None:
        if self._stop_handle is not None:
            self._stop_handle.cancel()
            self._stop_handle = None

    def _schedule_stop(self) -> None:
        self._cancel_stop()
        self._stop_handle = asyncio.get_running_loop().call_later(
            IDLE_GRACE_SECONDS, self._stop_if_idle
        )

    def _stop_if_idle(self) -> None:
        self._stop_handle = None
        if not self._subs and self._task and not self._task.done():
            _LOGGER.debug("No viewers left for %s; ending live session", self.serial)
            self._task.cancel()

    async def async_stop(self) -> None:
        """Stop the upstream session and end all clients."""
        self._cancel_stop()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for sub in list(self._subs):
            self._push(sub, None)

    # -- upstream ------------------------------------------------------

    async def _run(self) -> None:
        while self._subs:
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Zmodo live stream for %s failed: %s", self.serial, err)
            if self._subs:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        self._gop.clear()

    async def _pump(self) -> None:
        """Run one upstream session until it ends, stalls or lags."""
        live = await self._platform.start_live_session(self.serial)
        session_id = live.get("session", "")
        url = live.get("playlist")
        self.sessions_opened += 1
        ended = False
        try:
            if not url:
                raise ValueError(f"Live session response has no playlist: {live}")
            token = await self._platform.ensure_token()
            self._muxer.new_source()
            self._gop.clear()
            ws = await self._http.ws_connect(
                url,
                headers={"Authorization": f"Bearer {token}", "Origin": PLATFORM_ORIGIN},
                heartbeat=20,
                max_msg_size=WS_MAX_MSG_SIZE,
                **_WS_TIMEOUT,
            )
            _LOGGER.debug("Zmodo live session opened for %s", self.serial)
            try:
                await self._read_ws(ws)
            finally:
                # End the session before closing the socket, as the website
                # does, so the server stops sending promptly.
                ended = True
                await asyncio.shield(self._end_session(session_id))
                await self._close_ws(ws)
        finally:
            if not ended:
                await asyncio.shield(self._end_session(session_id))

    @staticmethod
    async def _close_ws(ws: aiohttp.ClientWebSocketResponse) -> None:
        """Close without waiting on a server that keeps sending.

        aiohttp's close() restarts its timeout for every message received;
        cancelling it aborts the underlying connection instead.
        """
        try:
            await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    async def _read_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        wall0: float | None = None
        ts0 = 0
        last_log = 0.0
        loop = asyncio.get_running_loop()
        while self._subs:
            msg = await asyncio.wait_for(ws.receive(), timeout=WS_IDLE_TIMEOUT)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    self.aes_key = parse_config(msg.data).get("aesKey") or self.aes_key
                except ValueError:
                    _LOGGER.debug("Ignoring text message: %s", msg.data[:200])
                continue
            if msg.type != aiohttp.WSMsgType.BINARY:
                _LOGGER.debug("Zmodo live session for %s ended: %s", self.serial, msg.type)
                return
            frame = parse_frame(msg.data, self.aes_key)

            # Lag watchdog: compare arrival time with the camera clock.
            now = loop.time()
            if wall0 is None:
                wall0, ts0 = now, frame.timestamp_us
            lag = (now - wall0) - (frame.timestamp_us - ts0) / 1e6
            if now - last_log > 30:
                last_log = now
                _LOGGER.debug(
                    "Zmodo %s: %d client(s), delivery lag %.1fs",
                    self.serial, len(self._subs), lag,
                )
            if lag > LAG_RESTART_SECONDS:
                self.lag_restarts += 1
                _LOGGER.warning(
                    "Zmodo live stream for %s is %.0fs behind; restarting session",
                    self.serial, lag,
                )
                return

            for tag, is_key in self._muxer.mux_tags(frame):
                self._broadcast(tag, is_key)

    async def _end_session(self, session_id: str) -> None:
        try:
            await self._platform.end_live_session(session_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ending live session for %s failed: %s", self.serial, err)

    # -- fan-out -------------------------------------------------------

    def _broadcast(self, tag: bytes, is_key: bool) -> None:
        config = self._muxer.config_tag
        is_config = tag is config
        if is_config:
            # New source: everyone re-syncs on its first keyframe.
            self._gop = []
            for sub in self._subs:
                sub.skip_to_key = True
            return
        if is_key:
            self._gop = [tag]
        elif self._gop:
            if len(self._gop) < GOP_CACHE_MAX:
                self._gop.append(tag)
            else:
                self._gop = []  # GOP too long to replay; joiners wait for next keyframe

        for sub in self._subs:
            if sub.skip_to_key:
                if not is_key or config is None:
                    continue
                if not self._push(sub, config):
                    self._overflow(sub)
                    continue
                sub.skip_to_key = False
            if not self._push(sub, tag):
                self._overflow(sub)

    @staticmethod
    def _overflow(sub: _Subscriber) -> None:
        """Client can't keep up: drop its backlog and resume at the next keyframe."""
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.skip_to_key = True

    @staticmethod
    def _push(sub: _Subscriber, item: bytes | None) -> bool:
        try:
            sub.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            if item is None:
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)
                return True
            return False
