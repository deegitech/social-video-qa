"""Frame timing and GOP analysis from synthetic packet lists (no FFmpeg)."""

from __future__ import annotations

from social_video_qa.parsers import Packet
from social_video_qa.probe import chroma_of, normalize_level, packet_stats

TB = 1 / 15360  # MP4 time base FFmpeg uses for 30 fps
FRAME = 512  # ticks per frame at 30 fps


def cfr_packets(n: int = 90, gop: int = 30, size: int = 1000) -> list[Packet]:
    return [Packet(pts=i * FRAME, dts=i * FRAME, duration=FRAME, size=size, key=i % gop == 0) for i in range(n)]


def test_cfr_stream() -> None:
    st = packet_stats(cfr_packets(), TB, duration_s=3.0)
    assert st.fps is not None and abs(st.fps - 30) < 1e-6
    assert st.irregular() == (0, 87, st.irregular()[2])
    assert not st.reordered
    assert st.leading_pictures == 0
    assert st.keyframes == 3
    assert st.starts_with_keyframe
    assert abs((st.max_keyint_s or 0) - 1.0) < 1e-6
    assert st.avg_kbps is not None and abs(st.avg_kbps - 90 * 1000 * 8 / 3.0 / 1000) < 1e-6


def test_stretched_first_frame_is_ignored() -> None:
    pk = cfr_packets()
    # -use_editlist 0 stretches the first frame by the AAC priming delay (1024 samples at 48 kHz)
    for p in pk[1:]:
        p.pts = p.dts = p.pts + 328
    st = packet_stats(pk, TB)
    bad, considered, _worst = st.irregular()
    assert bad == 0 and considered == 87


def test_vfr_detected() -> None:
    pk = cfr_packets()
    t = 0
    for i, p in enumerate(pk):
        p.pts = p.dts = t
        t += FRAME if i % 3 else FRAME * 2  # every third frame lasts twice as long
    st = packet_stats(pk, TB)
    bad, _considered, worst = st.irregular()
    assert bad > 20
    assert worst is not None and worst > 60


def test_millisecond_timestamps_are_not_vfr() -> None:
    # WebM/Matroska: 1/1000 time base, so 30 fps alternates 33 and 34 ms
    pk = [Packet(pts=round(i * 1000 / 30), dts=None, duration=None, size=500, key=i % 30 == 0) for i in range(90)]
    st = packet_stats(pk, 1 / 1000)
    assert st.irregular()[0] == 0
    pk[45].pts = (pk[45].pts or 0) + 10  # a real 10 ms glitch is still caught
    assert packet_stats(pk, 1 / 1000).irregular()[0] > 0


def test_b_frames_reordering() -> None:
    # decode order I P B B P B B ... (pts out of order)
    order = [0, 3, 1, 2, 6, 4, 5, 9, 7, 8]
    pk = [Packet(pts=o * FRAME, dts=(i - 2) * FRAME, duration=FRAME, size=500, key=i == 0) for i, o in enumerate(order)]
    st = packet_stats(pk, TB)
    assert st.reordered
    assert st.leading_pictures == 0  # closed GOP: nothing after the keyframe is shown before it


def test_open_gop_leading_pictures() -> None:
    # second keyframe (pts 30) is followed in decode order by B-frames shown before it (pts 28, 29)
    order = [(0, True), (3, False), (1, False), (2, False)] + [(i, False) for i in range(4, 28)]
    order += [(30, True), (28, False), (29, False), (31, False)]
    pk = [Packet(pts=o * FRAME, dts=i * FRAME, duration=FRAME, size=500, key=k) for i, (o, k) in enumerate(order)]
    st = packet_stats(pk, TB)
    assert st.leading_pictures == 2
    assert st.keyframes == 2


def test_peak_bitrate_window() -> None:
    pk = cfr_packets(n=90, size=1000)
    pk[45].size = 100_000  # one big frame in the middle second
    st = packet_stats(pk, TB, duration_s=3.0)
    assert st.peak_kbps_1s is not None
    assert st.peak_kbps_1s >= (29 * 1000 + 100_000) * 8 / 1000 - 1


def test_single_keyframe_gop_runs_to_end() -> None:
    st = packet_stats(cfr_packets(n=300, gop=1000), TB)
    assert st.keyframes == 1
    assert abs((st.max_keyint_s or 0) - 10.0) < 1e-6


def test_empty_packet_list() -> None:
    st = packet_stats([], TB)
    assert st.count == 0
    assert st.fps is None
    assert st.irregular() == (0, 0, None)


def test_chroma_and_bit_depth() -> None:
    assert chroma_of("yuv420p") == ("4:2:0", 8)
    assert chroma_of("yuvj420p") == ("4:2:0", 8)
    assert chroma_of("yuv420p10le") == ("4:2:0", 10)
    assert chroma_of("yuv422p10le") == ("4:2:2", 10)
    assert chroma_of("yuv444p") == ("4:4:4", 8)
    assert chroma_of("nv12") == ("4:2:0", 8)
    assert chroma_of("p010le") == ("4:2:0", 10)
    assert chroma_of("gray") == ("4:0:0", 8)
    assert chroma_of("rgb24") == ("4:4:4", None)
    assert chroma_of(None) == (None, None)
    assert chroma_of("weird") == (None, None)


def test_normalize_level() -> None:
    assert normalize_level("h264", 40) == 4.0
    assert normalize_level("h264", 31) == 3.1
    assert normalize_level("hevc", 120) == 4.0
    assert normalize_level("hevc", 153) == 5.1
    assert normalize_level("vp9", 40) is None
    assert normalize_level("h264", -99) is None
    assert normalize_level("h264", None) is None
