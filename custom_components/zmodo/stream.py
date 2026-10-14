"""Decode the user.zmodo.com live WebSocket stream and remux it to MPEG-TS.

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

from . import local_stream

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
    camera_seconds: int = -1  # camera wall clock (seconds since local midnight)

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
    h, m, sec = message[8], message[9], message[10]
    camera_seconds = h * 3600 + m * 60 + sec if h < 24 and m < 60 and sec < 60 else -1
    return ZmodoFrame(tag, ts_us, payload, camera_seconds)


def camera_clock_delay(camera_seconds: int, local_seconds: float) -> float | None:
    """Seconds between the camera's on-screen clock and local time (midnight-safe).

    Assumes the camera and Home Assistant use the same time zone; returns None
    for an implausible difference (e.g. a camera set to another zone).
    """
    if camera_seconds < 0:
        return None
    diff = (local_seconds - camera_seconds + 43200) % 86400 - 43200
    return diff if -60 < diff < 600 else None


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


# ----------------------------------------------------------------------
# MPEG-TS muxer
#
# We serve the live stream as MPEG-TS (not FLV) so one URL works for both
# delivery paths: Home Assistant's HLS pipeline reads it via ffmpeg, and the
# bundled go2rtc ingests it natively for WebRTC (go2rtc can't demux our FLV, so
# WebRTC silently fell back to HLS — the ~2.5 s penalty). TS fixes that: LAN
# viewers get low-latency WebRTC, remote/app viewers keep the HLS fallback.
# ----------------------------------------------------------------------

_PID_PAT = 0x0000
_PID_PMT = 0x1000
_PID_VIDEO = 0x0100
_STREAM_TYPE_H264 = 0x1B
_PROGRAM = 1
_PTS_HZ = 90000           # TS 90 kHz clock
_PTS_START = _PTS_HZ      # start PTS at 1 s so demuxers never see a zero/negative base
_AUD = b"\x00\x00\x00\x01\x09\xF0"   # H.264 access-unit delimiter


def _crc32_mpeg(data: bytes) -> int:
    """MPEG-2 CRC32 (poly 0x04C11DB7, init 0xFFFFFFFF, no reflection/xorout)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _psi_packet(pid: int, table_id: int, body: bytes) -> bytes:
    """One 188-byte TS packet carrying a PSI section (PAT/PMT), CC always 0."""
    section_length = len(body) + 4  # body + CRC32
    section = bytes([table_id]) + struct.pack(">H", 0xB000 | section_length) + body
    section += struct.pack(">I", _crc32_mpeg(section))
    payload = b"\x00" + section                       # pointer_field + section
    payload += b"\xFF" * (184 - len(payload))          # pad to a full packet
    return bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10]) + payload  # PUSI=1, AFC=payload-only


def _build_pat() -> bytes:
    body = struct.pack(">H", 1)                    # transport_stream_id
    body += bytes([0xC1, 0x00, 0x00])              # version 0, current, section 0/0
    body += struct.pack(">H", _PROGRAM)
    body += struct.pack(">H", 0xE000 | _PID_PMT)   # reserved + program_map_PID
    return _psi_packet(_PID_PAT, 0x00, body)


def _build_pmt() -> bytes:
    body = struct.pack(">H", _PROGRAM)
    body += bytes([0xC1, 0x00, 0x00])
    body += struct.pack(">H", 0xE000 | _PID_VIDEO)  # PCR_PID
    body += struct.pack(">H", 0xF000 | 0)           # program_info_length = 0
    body += bytes([_STREAM_TYPE_H264])
    body += struct.pack(">H", 0xE000 | _PID_VIDEO)  # elementary_PID
    body += struct.pack(">H", 0xF000 | 0)           # ES_info_length = 0
    return _psi_packet(_PID_PMT, 0x02, body)


def _pcr_bytes(pts90: int) -> bytes:
    base = pts90 & 0x1FFFFFFFF
    return bytes([
        (base >> 25) & 0xFF,
        (base >> 17) & 0xFF,
        (base >> 9) & 0xFF,
        (base >> 1) & 0xFF,
        ((base & 1) << 7) | 0x7E,   # reserved bits set, ext high bit 0
        0x00,                        # ext low byte 0
    ])


def _pes_header(pts90: int) -> bytes:
    pts = pts90 & 0x1FFFFFFFF
    return (
        b"\x00\x00\x01\xE0"          # start code + stream_id (video)
        + b"\x00\x00"                # PES_packet_length 0 = unbounded
        + bytes([0x80, 0x80, 5])     # marker, PTS-only flag, header len
        + bytes([
            0x21 | (((pts >> 30) & 0x7) << 1),
            (pts >> 22) & 0xFF,
            0x01 | (((pts >> 15) & 0x7F) << 1),
            (pts >> 7) & 0xFF,
            0x01 | ((pts & 0x7F) << 1),
        ])
    )


class TSMuxer:
    """Video-only MPEG-TS muxer for H.264 Annex-B access units.

    Same interface as the old FLV muxer so the hub is container-agnostic:
    ``new_source()`` resets per upstream session, ``mux_tags()`` returns
    ``[(bytes, is_key)]`` chunks (the first is ``config_tag`` = PAT+PMT, which the
    hub re-sends to mid-stream joiners), and ``file_header`` leads each client
    (empty for TS — PAT/PMT already opens the stream).
    """

    file_header = b""  # TS needs no separate file header; PAT/PMT is the start

    def __init__(self) -> None:
        self.config_tag: bytes = _build_pat() + _build_pmt()
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._cc = 0                 # video PID continuity counter
        self._t0: int | None = None
        self._pts_base = _PTS_START
        self._last_pts = -1
        self._need_config = True

    def new_source(self) -> None:
        """Prepare for frames from a new upstream session."""
        self._t0 = None
        self._pts_base = self._last_pts + _PTS_HZ // 10 if self._last_pts >= 0 else _PTS_START
        self._need_config = True

    def _build_es(self, frame: ZmodoFrame, nals: list[bytes]) -> bytes:
        parts = [_AUD]
        if frame.is_keyframe and self._sps and self._pps:
            parts.append(b"\x00\x00\x00\x01" + self._sps)
            parts.append(b"\x00\x00\x00\x01" + self._pps)
        for nal in nals:
            if nal[0] & 0x1F in (7, 8, 9):  # SPS/PPS re-inserted above; drop original AUDs
                continue
            parts.append(b"\x00\x00\x00\x01" + nal)
        return b"".join(parts)

    def _video_packets(self, es: bytes, pts90: int, keyframe: bool) -> bytes:
        payload = _pes_header(pts90) + es
        out = bytearray()
        pos, n, first = 0, len(payload), True
        while pos < n:
            cc = self._cc
            self._cc = (cc + 1) & 0x0F
            b1 = (0x40 if first else 0x00) | (_PID_VIDEO >> 8)
            b2 = _PID_VIDEO & 0xFF
            af = b""
            if first:                       # PCR (and random-access flag on keyframes)
                af = bytes([(0x40 if keyframe else 0x00) | 0x10]) + _pcr_bytes(pts90)
            avail = 184 - (1 + len(af)) if af else 184
            remaining = n - pos
            if remaining >= avail:
                chunk = payload[pos:pos + avail]
                pos += avail
                if af:
                    out += bytes([0x47, b1, b2, 0x30 | cc, len(af)]) + af + chunk
                else:
                    out += bytes([0x47, b1, b2, 0x10 | cc]) + chunk
            else:                            # last packet: pad with adaptation stuffing
                chunk = payload[pos:]
                pos = n
                if af:
                    af += b"\xFF" * (184 - 1 - len(af) - len(chunk))
                    out += bytes([0x47, b1, b2, 0x30 | cc, len(af)]) + af + chunk
                else:
                    fill = 184 - len(chunk)        # bytes for the adaptation field
                    af_body = b"" if fill == 1 else bytes([0x00]) + b"\xFF" * (fill - 2)
                    out += bytes([0x47, b1, b2, 0x30 | cc, len(af_body)]) + af_body + chunk
            first = False
        return bytes(out)

    def mux_tags(self, frame: ZmodoFrame) -> list[tuple[bytes, bool]]:
        """Return [(chunk, is_keyframe_or_config)] for one frame.

        Nothing is emitted until the first keyframe carrying SPS/PPS; PAT+PMT
        (config_tag) is emitted before the first keyframe of every source.
        """
        if not frame.is_video:
            return []
        nals = split_annexb(frame.payload)
        for nal in nals:
            t = nal[0] & 0x1F
            if t == 7:
                self._sps = nal
            elif t == 8:
                self._pps = nal

        out: list[tuple[bytes, bool]] = []
        if self._t0 is None:
            if not (frame.is_keyframe and self._sps and self._pps):
                return out  # wait for a decodable keyframe
            self._t0 = frame.timestamp_us

        pts = self._pts_base + max(0, (frame.timestamp_us - self._t0) * 9 // 100)
        pts = max(pts, self._last_pts + 1)  # strictly increasing

        if self._need_config:
            out.append((self.config_tag, True))
            self._need_config = False

        es = self._build_es(frame, nals)
        if es == _AUD:   # nothing but a delimiter — no slice data to send
            return out
        out.append((self._video_packets(es, pts, frame.is_keyframe), frame.is_keyframe))
        self._last_pts = pts
        return out

    def mux(self, frame: ZmodoFrame) -> bytes:
        """Single-consumer helper: file header + chunks as one byte string."""
        out = b"".join(chunk for chunk, _ in self.mux_tags(frame))
        if out and not getattr(self, "_sent_header", False):
            self._sent_header = True
            out = self.file_header + out
        return out


# ----------------------------------------------------------------------
# Shared live hub: one upstream Zmodo session per camera, many clients
# ----------------------------------------------------------------------

SUB_QUEUE_MAX = 60          # ~6 s at 10 fps; beyond this a client skips to the next keyframe
GOP_CACHE_MAX = SUB_QUEUE_MAX - 2  # frames kept since the last keyframe for instant joins
LAG_RESTART_SECONDS = 10.0  # frames arriving this far behind the camera clock -> new session
IDLE_GRACE_SECONDS = 5.0    # keep the upstream session briefly after the last client leaves
RETRY_DELAY_SECONDS = 3.0   # first retry; doubles per consecutive failure
RETRY_DELAY_MAX = 60.0


class _Subscriber:
    __slots__ = ("queue", "skip_to_key", "delivered")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        # True until the client is positioned on config + keyframe
        self.skip_to_key = True
        self.delivered = False


class LiveHub:
    """Fan one camera's live session out to any number of MPEG-TS clients."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        platform: Any,
        serial: str,
        aes_key: str = "",
        create_task=None,
        local_clock=None,
        local_host: str | None = None,
        local_op: int = local_stream.OP_START_HD,
        quality: str = "sd",
        allow_cloud: bool = True,
    ) -> None:
        self._http = session
        self._platform = platform
        self.serial = serial
        self.aes_key = aes_key
        # When set, pull video directly from the camera over the LAN (port 8000)
        # instead of the cloud. ``local_op`` picks the stream (HD mainstream vs
        # SD substream); ``quality`` is the label this hub serves; ``allow_cloud``
        # is False for HD (the cloud has no HD, so no silent drop to SD).
        self.local_host = local_host
        self.local_op = local_op
        self.quality = quality
        self.allow_cloud = allow_cloud
        self.active_source: str | None = None  # "local" or "cloud" while streaming
        self._create_task = create_task or asyncio.get_running_loop().create_task
        # Returns local seconds since midnight (HA's time zone); for diagnostics.
        self._local_clock = local_clock
        self.camera_delay: float | None = None  # smoothed end-to-Zmodo delay
        self._subs: set[_Subscriber] = set()
        self._muxer = TSMuxer()
        self._gop: list[bytes] = []
        self._task: asyncio.Task | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self.sessions_opened = 0
        self.lag_restarts = 0
        self._got_frames = False
        self._failures = 0          # consecutive failed upstream attempts (persists across viewers)
        self._next_attempt = 0.0    # loop time before which Zmodo is not retried

    # -- client side ---------------------------------------------------

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield MPEG-TS bytes for one client, starting at the latest keyframe."""
        if (
            self._failures
            and not self._gop
            and asyncio.get_running_loop().time() < self._next_attempt
        ):
            return  # backing off after Zmodo errors: fail fast, don't call Zmodo
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
            while True:
                item = await asyncio.wait_for(sub.queue.get(), timeout=WS_IDLE_TIMEOUT)
                if item is None:
                    return
                if not sub.delivered:
                    # Container header (if any) only once real video exists, so a
                    # relay can still answer with an HTTP error if the camera never
                    # starts. TS has no separate header (PAT/PMT leads); FLV did.
                    sub.delivered = True
                    item = self._muxer.file_header + item
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

    def _backoff(self) -> float:
        return min(RETRY_DELAY_MAX, RETRY_DELAY_SECONDS * 2 ** max(0, self._failures - 1))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while self._subs:
            wait = self._next_attempt - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
                if not self._subs:
                    break
            self._got_frames = False
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self._got_frames:
                    self._failures = 0
                self._failures += 1
                delay = self._backoff()
                log = _LOGGER.warning if self._failures in (1, 5) else _LOGGER.debug
                log(
                    "Zmodo live stream for %s failed (attempt %d, retrying in %.0fs): %s",
                    self.serial, self._failures, delay, err,
                )
                # Clients still waiting for their first frame get a clean
                # failure now instead of an empty stream later.
                for sub in list(self._subs):
                    if not sub.delivered and sub.queue.empty():
                        self._push(sub, None)
            else:
                if self._got_frames:
                    if self._failures:
                        _LOGGER.info("Zmodo live stream for %s recovered", self.serial)
                    self._failures = 0
                    delay = RETRY_DELAY_SECONDS
                else:
                    self._failures += 1   # session ended without any video
                    delay = self._backoff()
            self._next_attempt = loop.time() + delay
        self._gop.clear()

    async def _pump(self) -> None:
        """Run one upstream session (local LAN if configured, else cloud).

        A local path that never produces a frame (camera off-LAN / refused)
        falls back to the cloud within the same attempt; a local path that was
        working and then dropped re-raises, so the retry loop reconnects locally
        rather than thrashing to the cloud.
        """
        if self.local_host:
            try:
                await self._pump_local()
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                # Mid-stream drop, or an HD hub with no cloud equivalent:
                # re-raise so the retry loop reconnects locally rather than
                # silently serving SD cloud video from an "HD" entity.
                if self._got_frames or not self.allow_cloud:
                    raise
                _LOGGER.info(
                    "Local stream for %s unavailable (%s); falling back to cloud",
                    self.serial, err,
                )
        if not self.allow_cloud:
            raise RuntimeError("no cloud source for this stream (HD is LAN-only)")
        await self._pump_cloud()

    async def _pump_local(self) -> None:
        """Pull one live session straight from the camera over the LAN."""
        self.active_source = "local"
        self.sessions_opened += 1
        self._muxer.new_source()
        self._gop.clear()

        def _remember_key(key: str) -> None:
            self.aes_key = key

        frames = local_stream.stream_local(
            self.local_host,
            self.aes_key,
            stream_op=self.local_op,
            on_key=_remember_key,
        )
        try:
            await self._consume(frames.__anext__)
        finally:
            self.active_source = None
            await frames.aclose()

    async def _pump_cloud(self) -> None:
        """Run one cloud upstream session until it ends, stalls or lags."""
        self.active_source = "cloud"
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
                await self._consume(self._ws_reader(ws))
            finally:
                # End the session before closing the socket, as the website
                # does, so the server stops sending promptly.
                ended = True
                await asyncio.shield(self._end_session(session_id))
                await self._close_ws(ws)
        finally:
            self.active_source = None
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

    def _ws_reader(self, ws: aiohttp.ClientWebSocketResponse):
        """Message source for the cloud WebSocket: yields raw media frames.

        The leading TEXT config message updates the AES key in place; a
        non-binary message signals the session end (returns None).
        """
        async def next_message() -> bytes | None:
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=WS_IDLE_TIMEOUT)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self.aes_key = parse_config(msg.data).get("aesKey") or self.aes_key
                    except ValueError:
                        _LOGGER.debug("Ignoring text message: %s", msg.data[:200])
                    continue
                if msg.type != aiohttp.WSMsgType.BINARY:
                    _LOGGER.debug(
                        "Zmodo live session for %s ended: %s", self.serial, msg.type
                    )
                    return None
                return msg.data
        return next_message

    async def _consume(self, next_message) -> None:
        """Drive one upstream session: parse, watch for lag, mux, fan out.

        ``next_message`` is a zero-arg coroutine returning the next raw media
        frame (32-byte header + payload) or None when the session ends. Used by
        both the cloud WebSocket and the local LAN reader.
        """
        wall0: float | None = None
        ts0 = 0
        last_log = 0.0
        loop = asyncio.get_running_loop()
        while self._subs:
            msg = await next_message()
            if msg is None:
                return
            frame = parse_frame(msg, self.aes_key)
            self._got_frames = True

            # Lag watchdog: compare arrival time with the camera clock.
            now = loop.time()
            if wall0 is None:
                wall0, ts0 = now, frame.timestamp_us
            lag = (now - wall0) - (frame.timestamp_us - ts0) / 1e6
            if self._local_clock is not None:
                delay = camera_clock_delay(frame.camera_seconds, self._local_clock())
                if delay is not None:
                    self.camera_delay = (
                        delay if self.camera_delay is None
                        else 0.9 * self.camera_delay + 0.1 * delay
                    )
            if now - last_log > 30:
                last_log = now
                _LOGGER.debug(
                    "Zmodo %s: %d client(s), delivery lag %.1fs, camera clock delay %s",
                    self.serial, len(self._subs), lag,
                    "n/a" if self.camera_delay is None else f"{self.camera_delay:.1f}s",
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
