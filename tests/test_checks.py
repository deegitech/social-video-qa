"""Every check, on hand-built MediaInfo objects (no FFmpeg)."""

from __future__ import annotations

from typing import Any

import pytest

from social_video_qa.boxes import Track
from social_video_qa.checks import REGISTRY, CheckReport, fixable, needs_for, run_checks, window_for
from social_video_qa.probe import Loudness, MediaInfo, StartContent

from .conftest import make_media


def one(m: MediaInfo, cid: str, params: dict[str, Any] | None = None, **kw: Any):
    report = run_checks(m, {cid: params or {}}, file="clip.mp4", profile="test", **kw)
    assert len(report.results) == 1
    return report.results[0]


def status(m: MediaInfo, cid: str, params: dict[str, Any] | None = None) -> tuple[str, str | None]:
    r = one(m, cid, params)
    return r.status, r.level


def test_every_check_passes_on_the_reference_media(media: MediaInfo) -> None:
    params = {
        "file.size": {"max_mb": 100},
        "container.format": {"allowed": ["mp4"]},
        "container.streams": {"max_video": 1, "max_audio": 1},
        "duration": {"min_s": 3, "max_s": 60},
        "bitrate.total": {"max_kbps": 25000},
        "video.codec": {"allowed": ["h264"]},
        "video.profile": {"allowed": {"h264": ["High"]}},
        "video.level": {"max": {"h264": 4.2}},
        "video.pix_fmt": {"allowed": ["yuv420p"]},
        "video.chroma": {"allowed": ["4:2:0"]},
        "video.size": {"allowed": ["1080x1920"]},
        "video.resolution": {"max_width": 1920},
        "video.aspect": {"min": 0.5, "max": 1, "recommended": "9:16"},
        "video.fps": {"min": 23, "max": 60},
        "video.bitrate": {"max_kbps": 25000},
        "video.keyint": {"max_s": 2},
        "audio.codec": {"allowed": ["aac"]},
        "audio.profile": {"allowed": {"aac": ["LC"]}},
        "audio.sample_rate": {"allowed": [48000]},
        "audio.channels": {"allowed": [2]},
        "audio.bitrate": {"max_kbps": 130},
        "audio.loudness": {"target_lufs": -14},
        "audio.true_peak": {"max_dbtp": -1},
        "audio.hiss": {"max_lufs": -35},
    }
    rules = {cid: params.get(cid, {}) for cid in REGISTRY}
    report = run_checks(media, rules, file="clip.mp4", profile="test")
    bad = [(r.id, r.message) for r in report.results if r.status != "pass"]
    assert bad == []
    assert report.ok
    assert len(report.results) == len(REGISTRY)


def test_range_hard_soft_and_rounding_message(media: MediaInfo) -> None:
    media.duration_s = 2.0
    assert status(media, "duration", {"min_s": 3}) == ("fail", "error")
    media.duration_s = 200.0
    r = one(media, "duration", {"max_s": 600, "recommended_max_s": 180})
    assert (r.status, r.level) == ("fail", "warn")
    assert "recommended" in r.message
    media.audio.bitrate_kbps = 128.3
    r = one(media, "audio.bitrate", {"max_kbps": 128})
    assert r.status == "fail"
    assert "128.3" in r.message  # never "128 kbps is above the maximum 128 kbps"


def test_soft_failure_never_exceeds_rule_severity(media: MediaInfo) -> None:
    media.video.display_width, media.video.display_height = 720, 1280
    r = one(media, "video.size", {"recommended": ["1080x1920"], "severity": "info"})
    assert (r.status, r.level) == ("fail", "info")
    r = one(media, "video.size", {"recommended": ["1080x1920"]})
    assert (r.status, r.level) == ("fail", "warn")
    r = one(media, "video.size", {"allowed": ["1080x1920"]})
    assert (r.status, r.level) == ("fail", "error")


def test_container_checks(media: MediaInfo) -> None:
    media.boxes.moov_before_mdat = False
    assert status(media, "container.faststart") == ("fail", "error")
    media.boxes.tracks[0].edit_list_entries = 1
    r = one(media, "container.edit_list")
    assert r.status == "fail" and "video" in r.message
    media.boxes.tracks.append(Track(3, "tmcd", False, 0))
    r = one(media, "container.tracks_enabled")
    assert r.status == "fail" and "timecode" in r.message
    media.container = "mkv"
    assert status(media, "container.format", {"allowed": ["mp4", "mov"]}) == ("fail", "error")
    media.audio_streams = 2
    assert status(media, "container.streams", {"max_audio": 1}) == ("fail", "warn")
    media.video_streams = 0
    assert status(media, "container.streams", {}) == ("fail", "warn")


def test_box_checks_skip_for_matroska(media: MediaInfo) -> None:
    media.boxes.is_iso = False
    for cid in ("container.faststart", "container.edit_list", "container.tracks_enabled"):
        assert one(media, cid).status == "skip"


def test_video_codec_profile_level(media: MediaInfo) -> None:
    media.video.codec = "vp9"
    assert status(media, "video.codec", {"allowed": ["h264", "hevc"]}) == ("fail", "error")
    assert one(media, "video.profile", {"allowed": {"h264": ["High"]}}).status == "skip"
    media.video.codec, media.video.profile, media.video.level = "h264", "High 10", 5.1
    assert status(media, "video.profile", {"allowed": {"h264": ["High", "Main"]}}) == ("fail", "error")
    r = one(media, "video.level", {"max": {"h264": 4.0}})
    assert r.status == "fail" and "5.1" in r.message


def test_pixel_format_chroma_scan_sar_rotation(media: MediaInfo) -> None:
    media.video.pix_fmt, media.video.chroma = "yuv422p10le", "4:2:2"
    assert status(media, "video.pix_fmt", {"allowed": ["yuv420p"]}) == ("fail", "error")
    assert status(media, "video.chroma", {"allowed": ["4:2:0"]}) == ("fail", "error")
    media.video.field_order = "tt"
    assert status(media, "video.progressive") == ("fail", "error")
    media.video.field_order = None
    assert one(media, "video.progressive").status == "pass"
    media.video.sar = "4:3"
    assert status(media, "video.sar") == ("fail", "warn")
    media.video.rotation, media.video.width, media.video.height = 90, 1920, 1080
    r = one(media, "video.rotation")
    assert (r.status, r.level) == ("fail", "info")
    assert "90" in r.message


def test_size_resolution_aspect(media: MediaInfo) -> None:
    media.video.display_width, media.video.display_height = 2160, 3840
    r = one(media, "video.resolution", {"max_width": 1920})
    assert r.status == "fail" and "width 2160 > 1920" in r.message
    assert one(media, "video.aspect", {"recommended": "9:16"}).status == "pass"
    media.video.display_width, media.video.display_height = 1920, 1080
    r = one(media, "video.aspect", {"max": 1, "recommended": "9:16"})
    assert (r.status, r.level) == ("fail", "error")
    assert "16:9" in r.message
    r = one(media, "video.aspect", {"recommended": ["9:16", "1:1"]})
    assert (r.status, r.level) == ("fail", "warn")
    media.video.display_width, media.video.display_height = 1080, 1080
    assert one(media, "video.aspect", {"recommended": ["9:16", "1:1"]}).status == "pass"


def test_display_aspect_uses_sar() -> None:
    m = make_media()
    m.video.display_width, m.video.display_height, m.video.sar = 1440, 1080, "4:3"
    assert m.video.display_aspect == pytest.approx(16 / 9)


def test_fps_and_cfr(media: MediaInfo) -> None:
    media.packets.fps = 29.97
    assert one(media, "video.fps", {"min": 23, "max": 30}).status == "pass"
    media.packets.fps = 60.0
    assert status(media, "video.fps", {"max": 30}) == ("fail", "error")
    assert status(media, "video.fps", {"max": 60, "recommended_max": 30}) == ("fail", "warn")
    media.packets.intervals_s = [1 / 30, 1 / 30, 1 / 20, 1 / 30, 1 / 30, 1 / 20, 1 / 30, 1 / 30]
    r = one(media, "video.cfr")
    assert r.status == "fail" and "variable frame rate" in r.message
    assert one(media, "video.cfr", {"max_irregular_pct": 50}).status == "pass"


def test_fast_mode_skips_and_analysis_errors_surface(media: MediaInfo) -> None:
    media.measured = set()
    media.packets = None
    assert one(media, "video.cfr").status == "skip"
    assert one(media, "video.keyint", {"max_s": 2}).status == "skip"
    media.analysis_errors["loudness"] = "ebur128 filter missing"
    r = one(media, "audio.loudness", {"target_lufs": -14})
    assert (r.status, r.level) == ("error", "warn")
    assert "ebur128 filter missing" in r.message


def test_b_frames_and_gop(media: MediaInfo) -> None:
    media.packets.reordered = True
    r = one(media, "video.b_frames")
    assert (r.status, r.level) == ("fail", "warn")
    media.packets.leading_pictures = 4
    r = one(media, "video.closed_gop")
    assert r.status == "fail" and "4 picture(s)" in r.message
    media.packets.leading_pictures = 0
    media.encoder_settings = {"encoder": "x264", "open_gop": 1, "bframes": 3}
    assert one(media, "video.closed_gop").status == "fail"
    media.encoder_settings = None
    media.packets.starts_with_keyframe = False
    assert "keyframe" in one(media, "video.closed_gop").message
    media.packets.max_keyint_s = 8.0
    assert status(media, "video.keyint", {"max_s": 2}) == ("fail", "warn")


def test_b_frames_fallback_without_packets(media: MediaInfo) -> None:
    media.packets = None
    media.video.has_b_frames = 2
    assert one(media, "video.b_frames").status == "fail"
    media.video.has_b_frames = 0
    media.encoder_settings = {"encoder": "x264", "bframes": 3}
    assert "encoder settings" in one(media, "video.b_frames").message


def test_video_bitrate_mentions_peak(media: MediaInfo) -> None:
    media.video.bitrate_kbps = 30000
    r = one(media, "video.bitrate", {"max_kbps": 25000})
    assert r.status == "fail" and "peak" in r.message


def test_audio_presence_and_format(media: MediaInfo) -> None:
    media.audio.codec, media.audio.profile = "aac", "HE-AAC"
    assert status(media, "audio.profile", {"allowed": {"aac": ["LC"]}}) == ("fail", "warn")
    media.audio.sample_rate = 96000
    assert one(media, "audio.sample_rate", {"max_hz": 48000}).status == "fail"
    assert one(media, "audio.sample_rate", {"allowed": [44100, 48000]}).status == "fail"
    media.audio.sample_rate = 44100
    assert status(media, "audio.sample_rate", {"recommended": [48000]}) == ("fail", "warn")
    media.audio.channels = 6
    assert status(media, "audio.channels", {"allowed": [1, 2]}) == ("fail", "error")
    media.audio = None
    assert status(media, "audio.present") == ("fail", "error")
    for cid in ("audio.codec", "audio.loudness", "audio.hiss"):
        assert one(media, cid, {"target_lufs": -14} if cid == "audio.loudness" else {}).status == "skip"


def test_audio_bitrate_is_not_judged_on_silence(media: MediaInfo) -> None:
    media.audio.bitrate_kbps = 2.0  # AAC encodes digital silence at about 2 kbps
    params = {"recommended_min_kbps": 224}
    assert status(media, "audio.bitrate", params) == ("fail", "warn")
    media.loudness = Loudness(-70.0, None, 0.0, float("-inf"))
    r = one(media, "audio.bitrate", params)
    assert r.status == "skip" and "silent" in r.message
    media.loudness = None  # not measured (fast mode): judged on the number alone
    assert status(media, "audio.bitrate", params) == ("fail", "warn")


def test_loudness_true_peak_hiss_silence(media: MediaInfo) -> None:
    media.loudness = Loudness(-20.5, -30.5, 6.0, -0.2)
    r = one(media, "audio.loudness", {"target_lufs": -14, "tolerance_lu": 1})
    assert r.status == "fail" and "too quiet by 6.5 LU" in r.message
    assert one(media, "audio.true_peak", {"max_dbtp": -1}).status == "fail"
    media.hiss_lufs = -27.0
    r = one(media, "audio.hiss", {"max_lufs": -35})
    assert (r.status, r.level) == ("fail", "warn")
    # relative: -27.0 - (-20.5) = -6.5 LU above the programme
    assert one(media, "audio.hiss", {"max_relative_lu": -10}).status == "fail"
    assert one(media, "audio.hiss", {"max_relative_lu": -5}).status == "pass"
    media.loudness = Loudness(-70.0, None, 0.0, float("-inf"))
    assert status(media, "audio.silence") == ("fail", "info")
    assert one(media, "audio.loudness", {"target_lufs": -14}).status == "skip"
    assert one(media, "audio.true_peak", {"max_dbtp": -1}).status == "pass"
    assert one(media, "audio.hiss", {"max_lufs": -35}).status == "skip"


def test_opening_frames(media: MediaInfo) -> None:
    media.start = StartContent(3.0, 0.5, 0.5, [], [])
    assert status(media, "content.black_start", {"max_s": 0.1}) == ("fail", "warn")
    r = one(media, "content.frozen_start", {"max_s": 0.2})
    assert r.status == "pass" and "black" in r.message
    media.start = StartContent(3.0, 0.0, 3.0, [], [])
    r = one(media, "content.frozen_start", {"max_s": 1})
    assert r.status == "fail" and "at least" in r.message


def test_location_metadata(media: MediaInfo) -> None:
    media.location_tags = ["com.apple.quicktime.location.iso6709"]
    r = one(media, "metadata.location")
    assert (r.status, r.level) == ("fail", "warn")


def test_no_video_stream_is_a_file_level_error() -> None:
    m = make_media(video=None, video_streams=0)
    report = run_checks(m, {"video.codec": {"allowed": ["h264"]}, "duration": {}}, file="song.m4a", profile="p")
    assert not report.ok
    assert report.results == []
    assert report.error is not None and "audio-only" in report.error


def test_video_checks_skip_without_video_info() -> None:
    m = make_media()
    for cid in ("video.codec", "video.size", "video.cfr", "content.black_start"):
        m.video = None
        out = REGISTRY[cid].fn({"allowed": ["h264"]} if cid == "video.codec" else {}, m)
        assert out.kind == "skip", cid


def test_severity_overrides_skip_only_and_strict(media: MediaInfo) -> None:
    media.packets.reordered = True
    media.boxes.moov_before_mdat = False
    rules = {"video.b_frames": {}, "container.faststart": {"severity": "warn"}, "duration": {"max_s": 60}}
    report = run_checks(media, rules, file="f", profile="p")
    assert report.ok  # only warnings
    assert not run_checks(media, rules, file="f", profile="p", strict=True).ok
    report = run_checks(media, rules, file="f", profile="p", skip=["video.*"])
    assert next(r for r in report.results if r.id == "video.b_frames").status == "skip"
    report = run_checks(media, rules, file="f", profile="p", only=["duration"])
    assert [r.id for r in report.results if r.status != "skip"] == ["duration"]
    off = run_checks(media, {"video.b_frames": {"severity": "off"}}, file="f", profile="p")
    assert off.results[0].status == "skip"


def test_report_counts_and_failures(media: MediaInfo) -> None:
    media.duration_s = 1.0
    media.packets.reordered = True
    report = run_checks(
        media, {"duration": {"min_s": 3}, "video.b_frames": {}, "video.rotation": {}}, file="f", profile="p"
    )
    assert report.counts == {"error": 1, "warn": 1, "info": 0, "pass": 1, "skip": 0}
    assert [r.id for r in report.failures()] == ["duration", "video.b_frames"]
    assert not report.ok
    assert report.to_dict()["ok"] is False
    assert CheckReport(file="x", profile="p", error="unreadable").ok is False


def test_hint_only_on_failures(media: MediaInfo) -> None:
    assert one(media, "duration", {"max_s": 60}).hint is None
    media.duration_s = 61
    assert "--trim" in (one(media, "duration", {"max_s": 60}).hint or "")


def test_needs_and_window() -> None:
    rules = {"audio.hiss": {}, "video.cfr": {}, "content.black_start": {"window_s": 5}, "duration": {}}
    assert needs_for(rules) == {"hiss", "loudness", "packets", "start"}
    assert needs_for(rules, skip=["audio.*", "video.*", "content.*"]) == set()
    assert window_for(rules) == 5.0
    assert window_for({}) == 3.0


def test_fixability_map() -> None:
    assert fixable("container.faststart")
    assert fixable("duration")
    assert not fixable("audio.hiss")
    assert not fixable("content.black_start")
    for cid in REGISTRY:
        assert cid.count(".") <= 1


def test_registry_metadata_is_complete() -> None:
    for cid, cdef in REGISTRY.items():
        assert cdef.title and cdef.doc and cdef.hint, cid
        assert cdef.severity in ("error", "warn", "info"), cid
