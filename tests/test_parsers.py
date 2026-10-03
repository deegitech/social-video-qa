"""Parsers for FFmpeg/FFprobe text output (no FFmpeg needed)."""

from __future__ import annotations

import math

from social_video_qa.ffmpeg import parse_version
from social_video_qa.parsers import (
    parse_blackdetect,
    parse_ebur128,
    parse_encoder_settings,
    parse_fraction,
    parse_freezedetect,
    parse_loudnorm_json,
    parse_packets,
)

EBUR128 = """\
[Parsed_ebur128_0 @ 0x1] t: 3.9    TARGET:-23 LUFS    M: -21.8 S: -21.8     I: -21.8 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -21.8 LUFS
    Threshold: -31.8 LUFS

  Loudness range:
    LRA:         3.4 LU
    Threshold: -41.8 LUFS
    LRA low:   -23.0 LUFS
    LRA high:  -19.6 LUFS

  True peak:
    Peak:      -20.8 dBFS
[out#0/null @ 0x2] video:0KiB audio:756KiB subtitle:0KiB
"""

SILENT = """\
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -70.0 LUFS
    Threshold:   0.0 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold:   0.0 LUFS
    LRA low:     0.0 LUFS
    LRA high:    0.0 LUFS

  True peak:
    Peak:       -inf dBFS
"""


def test_parse_ebur128_summary() -> None:
    s = parse_ebur128(EBUR128)
    assert s is not None
    assert s.integrated_lufs == -21.8
    assert s.threshold_lufs == -31.8
    assert s.lra_lu == 3.4
    assert s.true_peak_dbtp == -20.8


def test_parse_ebur128_uses_last_summary_and_handles_silence() -> None:
    s = parse_ebur128(EBUR128 + SILENT)
    assert s is not None
    assert s.integrated_lufs == -70.0
    assert s.true_peak_dbtp == -math.inf


def test_parse_ebur128_missing() -> None:
    assert parse_ebur128("no summary here") is None


def test_parse_loudnorm_json() -> None:
    text = """[Parsed_loudnorm_0 @ 0x1]
{
\t"input_i" : "-21.82",
\t"input_tp" : "-20.79",
\t"input_lra" : "0.00",
\t"input_thresh" : "-31.82",
\t"output_i" : "-13.96",
\t"target_offset" : "-0.04"
}
[out#0/null @ 0x2] video:0KiB"""
    data = parse_loudnorm_json(text)
    assert data is not None
    assert data["input_i"] == "-21.82"
    assert data["target_offset"] == "-0.04"
    assert parse_loudnorm_json("nothing") is None
    assert parse_loudnorm_json("{ not json }") is None


def test_parse_black_and_freeze() -> None:
    err = (
        "[Parsed_freezedetect_1 @ 0x1] lavfi.freezedetect.freeze_start: 0\n"
        "[Parsed_blackdetect_0 @ 0x2] black_start:0 black_end:0.5 black_duration:0.5\n"
        "[Parsed_freezedetect_1 @ 0x1] lavfi.freezedetect.freeze_duration: 0.533\n"
        "[Parsed_freezedetect_1 @ 0x1] lavfi.freezedetect.freeze_end: 0.533\n"
        "[Parsed_freezedetect_1 @ 0x1] lavfi.freezedetect.freeze_start: 2.1\n"
    )
    blacks = parse_blackdetect(err)
    assert [(b.start, b.end, b.duration) for b in blacks] == [(0.0, 0.5, 0.5)]
    freezes = parse_freezedetect(err)
    assert [(f.start, f.duration, f.end) for f in freezes] == [(0.0, 0.533, 0.533), (2.1, None, None)]


def test_parse_x264_and_x265_settings() -> None:
    head = (
        b"\x00\x00x264 - core 164 r3108 - H.264/MPEG-4 AVC codec - options: "
        b"cabac=1 ref=3 bframes=3 b_pyramid=2 open_gop=1 keyint=250 rc=crf\x00"
    )
    s = parse_encoder_settings(head)
    assert s == {"encoder": "x264", "bframes": 3, "open_gop": 1, "keyint": 250, "b_pyramid": 2, "rc": "crf"}
    head = b"x265 (build 199) - 3.5 - options: cpuid=1 bframes=4 b-adapt=2 no-open-gop keyint=250\x00"
    s = parse_encoder_settings(head)
    assert s == {"encoder": "x265", "bframes": 4, "open_gop": 0, "keyint": 250}
    assert parse_encoder_settings(b"\x00" * 100) is None


def test_parse_packets() -> None:
    text = "pts=0|dts=-1024|duration=512|size=7226|flags=K__\npts=N/A|dts=512|duration=512|size=100|flags=___\n\n"
    pk = parse_packets(text)
    assert len(pk) == 2
    assert (pk[0].pts, pk[0].dts, pk[0].size, pk[0].key) == (0, -1024, 7226, True)
    assert pk[1].pts is None and not pk[1].key


def test_parse_fraction() -> None:
    assert parse_fraction("30000/1001") == 30000 / 1001
    assert parse_fraction("30/1") == 30.0
    assert parse_fraction("0/0") is None
    assert parse_fraction("N/A") is None
    assert parse_fraction(None) is None


def test_parse_version() -> None:
    assert parse_version("ffmpeg version 8.1.2 Copyright (c) 2000-2026") == ("8.1.2", (8, 1))
    assert parse_version("ffmpeg version n6.1.1 Copyright") == ("n6.1.1", (6, 1))
    assert parse_version("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright") == ("4.4.2-0ubuntu0.22.04.1", (4, 4))
    assert parse_version("ffmpeg version N-112345-gdeadbeef Copyright") == ("N-112345-gdeadbeef", None)
    assert parse_version("") == (None, None)
