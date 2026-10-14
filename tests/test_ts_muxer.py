"""Standalone test for the MPEG-TS muxer (zmodo.stream.TSMuxer).

Runnable without Home Assistant:  ``python -I tests/test_ts_muxer.py``

Generates a real H.264 elementary stream with ffmpeg, splits it into access
units, feeds them through TSMuxer exactly as the live hub does, then checks the
result is well-formed TS *and* that ffmpeg can demux and decode it — i.e. a real
player (ffmpeg/go2rtc) will accept our output. If ffmpeg/ffprobe (with libx264)
aren't available, it falls back to structural checks only.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types

ZMODO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "zmodo",
)


def _load_stream():
    pkg = types.ModuleType("zt"); pkg.__path__ = [ZMODO_DIR]
    sys.modules["zt"] = pkg
    # stream.py imports `.local_stream`; load it first under the same fake package
    for name in ("local_stream", "stream"):
        spec = importlib.util.spec_from_file_location(f"zt.{name}", os.path.join(ZMODO_DIR, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"zt.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["zt.stream"]


def _nals(data: bytes) -> list[bytes]:
    """Split an Annex-B stream into NAL units (start codes removed)."""
    out, i, n, start = [], 0, len(data), -1
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start >= 0:
                end = i - 1 if data[i - 1] == 0 else i
                out.append(data[start:end])
            i += 3
            start = i
        else:
            i += 1
    if 0 <= start < n:
        out.append(data[start:])
    return [x for x in out if x]


def _access_units(nals: list[bytes]) -> list[list[bytes]]:
    """Group NALs into access units (new AU at each VCL NAL after a VCL NAL)."""
    groups, cur, has_vcl = [], [], False
    for nal in nals:
        is_vcl = 1 <= (nal[0] & 0x1F) <= 5
        if is_vcl and has_vcl:
            groups.append(cur); cur, has_vcl = [], False
        cur.append(nal)
        has_vcl = has_vcl or is_vcl
    if cur:
        groups.append(cur)
    return groups


def _structural_checks(S, ts: bytes, n_frames: int) -> None:
    assert ts, "muxer produced no output"
    assert len(ts) % 188 == 0, f"output not 188-aligned ({len(ts)} bytes)"
    assert all(ts[i] == 0x47 for i in range(0, len(ts), 188)), "missing TS sync bytes"
    # PAT/PMT present on their PIDs
    pids = {((ts[i + 1] & 0x1F) << 8) | ts[i + 2] for i in range(0, len(ts), 188)}
    assert S._PID_PAT in pids and S._PID_PMT in pids and S._PID_VIDEO in pids, f"PIDs: {pids}"
    # PAT CRC is valid (catches section/CRC mistakes). Section = table_id + 2
    # length bytes + body + 4-byte CRC; section_length counts body+CRC.
    pat = S._build_pat()
    sec_len = ((pat[6] & 0x0F) << 8) | pat[7]
    section = pat[5:5 + 3 + sec_len]
    body, stored_crc = section[:-4], int.from_bytes(section[-4:], "big")
    assert S._crc32_mpeg(body) == stored_crc, "PAT CRC invalid"
    print(f"structural OK: {len(ts)//188} TS packets, PIDs {sorted(pids)}, PAT CRC valid")


def main() -> int:
    S = _load_stream()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")

    with tempfile.TemporaryDirectory() as tmp:
        h264 = os.path.join(tmp, "src.h264")
        have_src = False
        if ffmpeg:
            r = subprocess.run(
                [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                 "-t", "2", "-c:v", "libx264", "-profile:v", "main", "-bf", "0", "-g", "10",
                 "-pix_fmt", "yuv420p", "-f", "h264", "-y", h264],
                capture_output=True, text=True,
            )
            have_src = r.returncode == 0 and os.path.getsize(h264) > 0
            if not have_src:
                print(f"[skip] ffmpeg could not make H.264 (libx264 missing?): {r.stderr.strip()[:150]}")

        if not have_src:
            # Fabricate a minimal frame so the muxer's packetiser is still exercised.
            S_mux = S.TSMuxer()
            sps = bytes([0x67, 0x42, 0x00, 0x0A, 0xFF])  # not a decodable SPS, structure only
            pps = bytes([0x68, 0xCE, 0x3C, 0x80])
            idr = bytes([0x65]) + b"\x00" * 400
            payload = b"".join(b"\x00\x00\x00\x01" + n for n in (sps, pps, idr))
            frame = S.ZmodoFrame(S.TAG_KEYFRAME, 0, payload)
            ts = b"".join(c for c, _ in S_mux.mux_tags(frame))
            _structural_checks(S, ts, 1)
            print("test_ts_muxer OK (structural only; install ffmpeg+libx264 for decode test)")
            return 0

        aus = _access_units(_nals(open(h264, "rb").read()))
        assert len(aus) >= 10, f"expected several frames, got {len(aus)}"

        muxer = S.TSMuxer()
        out = bytearray()
        keyframes = 0
        for idx, au in enumerate(aus):
            is_key = any((n[0] & 0x1F) == 5 for n in au)
            keyframes += is_key
            payload = b"".join(b"\x00\x00\x00\x01" + n for n in au)
            tag = S.TAG_KEYFRAME if is_key else S.TAG_PFRAME
            frame = S.ZmodoFrame(tag, idx * 100_000, payload)
            for chunk, _ in muxer.mux_tags(frame):
                out += chunk
        ts = bytes(out)

        _structural_checks(S, ts, len(aus))
        assert keyframes >= 2, f"expected >=2 keyframes (g=10, ~20 frames), got {keyframes}"

        ts_path = os.path.join(tmp, "out.ts")
        open(ts_path, "wb").write(ts)

        # Decode the whole thing — proves a real player accepts our TS.
        dec = subprocess.run([ffmpeg, "-v", "error", "-i", ts_path, "-f", "null", "-"],
                             capture_output=True, text=True)
        assert dec.returncode == 0, f"ffmpeg failed to decode our TS:\n{dec.stderr.strip()[:300]}"
        assert "error" not in dec.stderr.lower(), f"ffmpeg decode errors:\n{dec.stderr.strip()[:300]}"

        if ffprobe:
            codec = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", ts_path],
                capture_output=True, text=True).stdout.split()[0:1]
            codec = codec[0] if codec else ""
            assert codec == "h264", f"expected h264, ffprobe saw {codec!r}"
            nb = subprocess.run(
                [ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", ts_path],
                capture_output=True, text=True).stdout.split()[0]
            assert abs(int(nb) - len(aus)) <= 2, f"frame count {nb} vs fed {len(aus)}"
            print(f"decode OK: ffmpeg decoded clean; ffprobe sees {codec}, {nb} frames "
                  f"(fed {len(aus)}, {keyframes} keyframes)")
        print("test_ts_muxer OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
