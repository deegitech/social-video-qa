"""Fix planning and command building on hand-built MediaInfo objects (no FFmpeg)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from social_video_qa.checks import run_checks
from social_video_qa.ffmpeg import Runner
from social_video_qa.fix import (
    PREVIEW_MEASUREMENTS,
    FixError,
    FixOptions,
    build_encode_args,
    choose_fps,
    default_output,
    describe_plan,
    loudnorm_filter,
    output_for,
    plan_fix,
    square_pixel_size,
    video_filter,
)
from social_video_qa.probe import Loudness, MediaInfo
from social_video_qa.profiles import Profile, load_profile

from .conftest import make_media

RUNNER = Runner(ffmpeg="ffmpeg", ffprobe="ffprobe", threads=2, nice=0)


def plan_for(m: MediaInfo, profile: str = "instagram-reels-api", **opts):
    prof = load_profile(profile)
    options = FixOptions(**opts)
    report = run_checks(m, prof.rules, file=m.path, profile=prof.id, skip=options.skip, only=options.only)
    return plan_fix(m, report, prof, options, "out.mp4")


def test_passing_file_needs_nothing(media: MediaInfo) -> None:
    plan = plan_for(media)
    assert plan.mode == "none"
    assert plan.blockers == []


def test_faststart_only_is_a_remux(media: MediaInfo) -> None:
    media.boxes.moov_before_mdat = False
    media.location_tags = ["location"]
    plan = plan_for(media)
    assert (plan.mode, plan.video_action, plan.audio_action) == ("remux", "copy", "copy")
    assert set(plan.reasons["remux"]) == {"container.faststart", "metadata.location"}
    args = build_encode_args(plan, media, RUNNER, "out.mp4")
    assert "-c:v" in args and args[args.index("-c:v") + 1] == "copy"
    assert "+faststart" in args and "-map_metadata" in args


def test_edit_list_without_b_frames_is_a_remux(media: MediaInfo) -> None:
    media.boxes.tracks[1].edit_list_entries = 1  # AAC priming edit list on the audio track
    plan = plan_for(media)
    assert plan.mode == "remux"
    assert plan.container["no_edit_list"]
    args = build_encode_args(plan, media, RUNNER, "out.mp4")
    assert args[args.index("-use_editlist") + 1] == "0"


def test_edit_list_with_b_frames_needs_reencode(media: MediaInfo) -> None:
    media.boxes.tracks[0].edit_list_entries = 1
    media.packets.reordered = True
    plan = plan_for(media)
    assert plan.mode == "full"
    assert plan.video_action == "encode"
    assert plan.video["bframes"] == 0


def test_loudness_is_an_audio_only_fix(media: MediaInfo) -> None:
    media.loudness = Loudness(-24.0, -34.0, 4.0, -9.0)
    plan = plan_for(media)
    assert (plan.mode, plan.video_action, plan.audio_action) == ("audio", "copy", "encode")
    assert plan.loudness == {"target_lufs": -14.0, "true_peak_dbtp": -1.5, "lra": 11.0}
    assert plan.audio["bitrate_kbps"] == 128


def test_missing_audio_gets_silent_track_for_app_store() -> None:
    m = make_media(audio=None, audio_streams=0, duration_s=20.0)
    m.video.display_width, m.video.display_height = 886, 1920
    m.packets.fps = 30.0
    plan = plan_for(m, "app-store-preview-iphone")
    assert plan.audio_action == "silent"
    args = build_encode_args(plan, m, RUNNER, "out.mp4")
    assert any(a.startswith("anullsrc=r=48000:cl=stereo") for a in args)
    assert "1:a:0" in args


def test_missing_audio_is_fine_for_instagram() -> None:
    m = make_media(audio=None, audio_streams=0)
    assert plan_for(m).audio_action == "none"


def test_app_store_full_plan_uses_cbr_level_and_size() -> None:
    m = make_media(duration_s=20.0)
    m.video.display_width, m.video.display_height = 1280, 720
    m.packets.fps = 60.0
    plan = plan_for(m, "app-store-preview-iphone", fit="blur")
    assert plan.mode == "full"
    v = plan.video
    assert (v["width"], v["height"], v["fit"]) == (886, 1920, "blur")
    assert v["fps"] == "30"
    assert v["level"] == "4.0"
    assert v["rate"] == {"mode": "cbr", "kbps": 10000.0}
    args = build_encode_args(plan, m, RUNNER, "out.mp4")
    assert args[args.index("-x264-params") + 1] == "nal-hrd=cbr:force-cfr=1"
    assert args[args.index("-level:v") + 1] == "4.0"
    assert plan.audio["bitrate_kbps"] == 256


def test_too_long_needs_trim() -> None:
    m = make_media(duration_s=45.0)
    m.video.display_width, m.video.display_height = 886, 1920
    plan = plan_for(m, "app-store-preview-iphone")
    assert plan.blockers and "--trim" in plan.blockers[0]
    plan = plan_for(m, "app-store-preview-iphone", trim=True)
    assert plan.blockers == []
    assert plan.trim_s == pytest.approx(29.9)
    assert plan.mode == "full" and plan.audio_action == "encode"
    args = build_encode_args(plan, m, RUNNER, "out.mp4", PREVIEW_MEASUREMENTS)
    assert args[args.index("-t") + 1] == "29.900"
    assert any("afade=t=out" in a for a in args)


def test_too_short_is_a_blocker() -> None:
    m = make_media(duration_s=2.0)
    plan = plan_for(m)
    assert plan.blockers and "duration" in plan.blockers[0]


def test_unfixable_warning_is_reported_but_does_not_block(media: MediaInfo) -> None:
    media.hiss_lufs = -25.0
    media.boxes.moov_before_mdat = False
    plan = plan_for(media)
    assert plan.mode == "remux"
    assert [r.id for r in plan.unfixed] == ["audio.hiss"]
    assert plan.blockers == []


def test_strict_refuses_up_front_when_an_unfixable_warning_remains(media: MediaInfo) -> None:
    media.hiss_lufs = -25.0
    media.boxes.moov_before_mdat = False
    plan = plan_for(media, strict=True)
    assert [r.id for r in plan.unfixed] == ["audio.hiss"]
    assert plan.blockers and "audio.hiss" in plan.blockers[0] and "--strict" in plan.blockers[0]
    m = make_media(duration_s=200.0)  # TikTok recommends <= 180 s (a warning); --trim only cuts to the hard maximum
    assert plan_for(m, "tiktok").blockers == []
    assert "--strict" in plan_for(m, "tiktok", strict=True).blockers[0]


def test_forced_remux_cannot_fix_audio(media: MediaInfo) -> None:
    media.loudness = Loudness(-24.0, -34.0, 4.0, -9.0)
    plan = plan_for(media, mode="remux")
    assert any("audio.loudness" in b for b in plan.blockers)


def test_forced_full_mode(media: MediaInfo) -> None:
    plan = plan_for(media, mode="full")
    assert (plan.mode, plan.video_action, plan.audio_action) == ("full", "encode", "encode")
    assert (plan.video["width"], plan.video["height"]) == (1080, 1920)  # size kept when it passes


def test_non_mp4_codecs_are_reencoded(media: MediaInfo) -> None:
    media.video.codec = "prores"
    media.audio.codec = "pcm_s16le"
    media.boxes.moov_before_mdat = False
    prof = Profile(id="t", title="t", rules={"container.faststart": {}})
    report = run_checks(media, prof.rules, file=media.path, profile=prof.id)
    plan = plan_fix(media, report, prof, FixOptions(), "out.mp4")
    assert (plan.mode, plan.video_action, plan.audio_action) == ("full", "encode", "encode")
    assert sum("cannot be copied" in n for n in plan.notes) == 2
    plan = plan_fix(media, report, prof, FixOptions(mode="remux"), "out.mp4")
    assert len(plan.blockers) == 2  # remux was forced, but neither stream fits in MP4


def test_b_frames_alone_do_not_trigger_a_rewrite_when_nothing_fails(media: MediaInfo) -> None:
    media.packets.reordered = True
    assert plan_for(media, skip=["video.b_frames"]).mode == "none"


def test_size_budget_caps_maxrate() -> None:
    m = make_media(duration_s=880.0)
    m.packets.reordered = True  # forces a re-encode for Instagram
    plan = plan_for(m)
    cap = plan.video["rate"]["maxrate_kbps"]
    # 300 MB over 880 s, minus audio, with 8% margin
    assert cap <= 300 * 8000 / 880 * 0.92 - 128 + 1
    assert plan.blockers == []


def test_skip_excludes_checks_from_the_plan(media: MediaInfo) -> None:
    media.packets.reordered = True
    assert plan_for(media).mode == "full"
    assert plan_for(media, skip=["video.b_frames"]).mode == "none"


def test_interlaced_source_is_deinterlaced(media: MediaInfo) -> None:
    media.video.field_order = "tt"
    plan = plan_for(media)
    assert plan.video["deinterlace"]
    assert video_filter(plan, media).startswith("[0:0]bwdif=")


@pytest.mark.parametrize(
    ("src", "bounds", "expected"),
    [
        (30.0, (23, 60, None, None), Fraction(30)),
        (29.97, (23, 60, None, None), Fraction(30000, 1001)),
        (60.0, (None, 30, 25, None), Fraction(30)),
        (59.94, (None, 30, 25, None), Fraction(30000, 1001)),
        (50.0, (None, 30, 25, None), Fraction(25)),
        (24.0, (None, 30, 25, None), Fraction(25)),
        (120.0, (23, 60, None, None), Fraction(60)),
        (15.0, (23, 60, None, None), Fraction(24000, 1001)),
        (None, (23, 60, None, None), Fraction(30)),
    ],
)
def test_choose_fps(src: float | None, bounds: tuple, expected: Fraction) -> None:
    assert choose_fps(src, *bounds) == expected


def test_video_filter_variants(media: MediaInfo) -> None:
    media.video.display_width, media.video.display_height = 1920, 1080
    media.packets.reordered = True
    for fit, fragment in (
        ("pad", "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"),
        ("crop", "crop=1080:1920"),
        ("blur", "boxblur=8:2"),
    ):
        plan = plan_for(media, fit=fit, mode="full")
        plan.video.update({"width": 1080, "height": 1920})
        graph = video_filter(plan, media)
        assert fragment in graph and graph.endswith("setsar=1[v]")
    media.video.display_width, media.video.display_height = 720, 1280
    plan = plan_for(media, mode="full")
    plan.video.update({"width": 1080, "height": 1920})
    assert "scale=1080:1920:flags=lanczos" in video_filter(plan, media)


def test_square_pixel_size() -> None:
    m = make_media()
    v = m.video
    v.width, v.height, v.display_width, v.display_height = 1440, 1080, 1440, 1080
    assert square_pixel_size(v) == (1440, 1080)  # SAR unset
    v.sar = "1:1"
    assert square_pixel_size(v) == (1440, 1080)
    v.sar = "4:3"  # anamorphic 16:9
    assert square_pixel_size(v) == (1920, 1080)
    v.rotation, v.display_width, v.display_height = 90, 1080, 1440  # the stretched width is now vertical
    assert square_pixel_size(v) == (1080, 1920)


def test_anamorphic_source_is_resampled_not_relabelled() -> None:
    m = make_media(duration_s=10.0)
    v = m.video
    v.width, v.height, v.display_width, v.display_height, v.sar = 1440, 1080, 1440, 1080, "4:3"
    plan = plan_for(m, "tiktok")  # 16:9 on screen: aspect warns, so the video is fitted to 1080x1920
    assert plan.video_action == "encode" and plan.video["resize"] == "pad"
    graph = video_filter(plan, m)
    assert graph.startswith("[0:0]scale=1920:1080:flags=lanczos,setsar=1,scale=1080:1920:")
    assert any("non-square pixels" in n for n in plan.notes)
    # without a size target the square-pixel size is kept
    plan = plan_for(m, "instagram-reels-api", mode="full", skip=["video.*"])
    assert (plan.video["width"], plan.video["height"]) == (1920, 1080)
    assert video_filter(plan, m) == "[0:0]scale=1920:1080:flags=lanczos,setsar=1,fps=30,format=yuv420p,setsar=1[v]"


def test_hdr_source_gets_a_plan_note(media: MediaInfo) -> None:
    media.packets.reordered = True
    assert not any("HDR" in n for n in plan_for(media).notes)
    media.video.color_transfer = "arib-std-b67"
    assert any("HDR (arib-std-b67)" in n and "tone mapping" in n for n in plan_for(media).notes)


def test_loudnorm_second_pass(media: MediaInfo) -> None:
    media.loudness = Loudness(-24.0, -34.0, 4.0, -9.0)
    plan = plan_for(media)
    measured = {
        "input_i": "-24.0",
        "input_tp": "-9.0",
        "input_lra": "15.0",
        "input_thresh": "-34.0",
        "target_offset": "0.1",
    }
    flt = loudnorm_filter(plan, measured)
    assert flt is not None
    assert "measured_I=-24:measured_TP=-9:" in flt and "offset=0.1:linear=true" in flt
    assert ":LRA=16:" in flt  # raised above the measured range so linear mode stays possible
    assert loudnorm_filter(plan, {**measured, "input_i": "-inf"}) is None  # silence is left alone
    assert loudnorm_filter(plan, None) is None
    assert "<pass1-I>" in (loudnorm_filter(plan, PREVIEW_MEASUREMENTS) or "")


@pytest.mark.parametrize("bad", ["-9.0,volume=10", "-9:af=x", "nan", "inf", ""])
def test_loudnorm_measurements_cannot_change_the_filtergraph(media: MediaInfo, bad: str) -> None:
    media.loudness = Loudness(-24.0, -34.0, 4.0, -9.0)
    plan = plan_for(media)
    measured = {"input_i": "-24.0", "input_tp": bad, "input_lra": "5", "input_thresh": "-34", "target_offset": "0"}
    with pytest.raises(FixError, match="unexpected loudnorm measurement"):
        loudnorm_filter(plan, measured)
    measured = {"input_i": "-24.0", "input_lra": "5", "input_thresh": "-34", "target_offset": "0"}  # no input_tp
    with pytest.raises(FixError):
        loudnorm_filter(plan, measured)


def test_hardened_input_and_stripping_in_commands(media: MediaInfo) -> None:
    media.packets.reordered = True
    plan = plan_for(media)
    args = build_encode_args(plan, media, RUNNER, "out.mp4", PREVIEW_MEASUREMENTS)
    i = args.index("-i")
    assert args[i - 4 : i + 2] == [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov,matroska,avi,mpegts",
        "-i",
        "file:clip.mp4",
    ]
    assert "-nostdin" in args
    assert args[-1] == "out.mp4"
    assert plan_for(media, keep_metadata=True).strip_metadata is False


def test_hevc_copy_gets_hvc1_tag(media: MediaInfo) -> None:
    media.video.codec = "hevc"
    media.boxes.moov_before_mdat = False
    plan = plan_for(media)
    args = build_encode_args(plan, media, RUNNER, "out.mp4")
    assert args[args.index("-tag:v") + 1] == "hvc1"


def test_recipe_changes_with_settings(media: MediaInfo) -> None:
    media.packets.reordered = True
    a = plan_for(media).recipe
    assert a == plan_for(media).recipe
    assert a != plan_for(media, crf=26).recipe


def test_describe_plan_lines(media: MediaInfo) -> None:
    media.packets.reordered = True
    media.loudness = Loudness(-24.0, -34.0, 4.0, -9.0)
    lines = describe_plan(plan_for(media))
    assert lines[0].startswith("video: re-encode with libx264 high")
    assert "two-pass loudnorm to -14 LUFS" in lines[1]
    assert lines[2].startswith("container: MP4, moov first, no edit list, metadata stripped")


def test_default_output_names() -> None:
    assert default_output("in/clip.mov", "tiktok", FixOptions()) == "in/clip.tiktok.mp4"
    assert default_output("clip.mp4", "tiktok", FixOptions(out_dir="out")) == "out/clip.tiktok.mp4"
    assert default_output("clip.mp4", "tiktok", FixOptions(output="x.mp4")) == "x.mp4"
    mov = Profile(id="house", title="house", fix={"container": {"format": "mov"}})
    assert output_for("in/clip.mp4", mov, FixOptions()) == "in/clip.house.mov"
