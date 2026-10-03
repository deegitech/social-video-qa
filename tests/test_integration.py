"""End-to-end tests on tiny synthetic videos generated with FFmpeg's lavfi sources.

Skipped automatically when ffmpeg/ffprobe are not installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from social_video_qa import fix as fix_module
from social_video_qa.checks import needs_for, run_checks, window_for
from social_video_qa.cli import main
from social_video_qa.doctor import diagnose, render
from social_video_qa.ffmpeg import Runner
from social_video_qa.fix import LOCK_NAME, MANIFEST_NAME, FixError, FixOptions, fix_file
from social_video_qa.probe import NEEDS_ALL, ProbeError, analyze
from social_video_qa.profiles import load_profile
from social_video_qa.util import sha256_file

from .conftest import LEVELLED_SINE, SAFE_AUDIO, SAFE_MUX, SAFE_VIDEO, ffmpeg, needs_ffmpeg, src_sine, src_video

pytestmark = [needs_ffmpeg, pytest.mark.ffmpeg]


def check(path: str, profile: str, runner: Runner, **kw):
    prof = load_profile(profile)
    media = analyze(path, runner, needs_for(prof.rules), start_window_s=window_for(prof.rules))
    return run_checks(media, prof.rules, file=path, profile=prof.id, **kw), media


def failing(report) -> dict[str, str]:
    return {r.id: r.level for r in report.results if r.failed}


# ---------------------------------------------------------------------- media
@pytest.fixture(scope="session")
def moov_last(make: Callable[..., str]) -> str:
    return make("moov_last.mp4", *src_video(), *src_sine(), "-af", LEVELLED_SINE, *SAFE_VIDEO, *SAFE_AUDIO,
                "-use_editlist", "0", "-shortest")  # fmt: skip


@pytest.fixture(scope="session")
def b_frames(make: Callable[..., str]) -> str:
    """x264 defaults: B-frames, and FFmpeg's MP4 muxer writes edit lists."""
    return make("bframes.mp4", *src_video(), *src_sine(), "-af", LEVELLED_SINE, "-c:v", "libx264", "-preset",
                "veryfast", "-pix_fmt", "yuv420p", *SAFE_AUDIO, "-movflags", "+faststart", "-shortest")  # fmt: skip


@pytest.fixture(scope="session")
def open_gop(make: Callable[..., str]) -> str:
    return make("open_gop.mp4", *src_video(seconds=6), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt",
                "yuv420p", "-bf", "3", "-x264-params", "open-gop=1:keyint=30:min-keyint=30:scenecut=0",
                "-movflags", "+faststart")  # fmt: skip


@pytest.fixture(scope="session")
def loud(make: Callable[..., str]) -> str:
    """Video is fine; audio is far too loud and peaks at full scale."""
    return make("loud.mp4", *src_video(), *src_sine(), "-af", "volume=16", *SAFE_VIDEO, *SAFE_AUDIO, *SAFE_MUX,
                "-shortest")  # fmt: skip


@pytest.fixture(scope="session")
def hissy(make: Callable[..., str]) -> str:
    graph = ("sine=frequency=440:sample_rate=48000,volume=4[s];anoisesrc=r=48000:a=0.1:c=white[n];"
             "[s][n]amix=inputs=2:normalize=0[out0]")  # fmt: skip
    return make("hissy.mp4", *src_video(), "-f", "lavfi", "-t", "4", "-i", graph, "-af", LEVELLED_SINE,
                *SAFE_VIDEO, *SAFE_AUDIO, *SAFE_MUX, "-shortest")  # fmt: skip


@pytest.fixture(scope="session")
def black_start(make: Callable[..., str]) -> str:
    graph = "color=c=black:s=360x640:r=30:d=0.5[b];testsrc2=s=360x640:r=30:d=3.5[t];[b][t]concat=n=2:v=1:a=0[out0]"
    return make("black_start.mp4", "-f", "lavfi", "-i", graph, *SAFE_VIDEO, *SAFE_MUX)


@pytest.fixture(scope="session")
def frozen_start(make: Callable[..., str]) -> str:
    graph = ("testsrc2=s=360x640:r=30:d=4,trim=end_frame=1,loop=loop=44:size=1:start=0,setpts=N/30/TB[f];"
             "testsrc2=s=360x640:r=30:d=3[t];[f][t]concat=n=2:v=1:a=0[out0]")  # fmt: skip
    return make("frozen_start.mp4", "-f", "lavfi", "-i", graph, *SAFE_VIDEO, *SAFE_MUX)


@pytest.fixture(scope="session")
def located(make: Callable[..., str]) -> str:
    return make("located.mp4", *src_video(), *src_sine(), "-af", LEVELLED_SINE, *SAFE_VIDEO, *SAFE_AUDIO, *SAFE_MUX,
                "-metadata", "location=+00.0000+000.0000/", "-shortest")  # fmt: skip


@pytest.fixture(scope="session")
def vfr(make: Callable[..., str], ffmpeg_version: tuple[int, int]) -> str:
    passthrough = ["-fps_mode", "passthrough"] if ffmpeg_version >= (5, 1) else ["-vsync", "passthrough"]
    # drop every fifth frame and keep the original timestamps: the classic "dropped frames" VFR pattern
    return make("vfr.mp4", *src_video(), "-vf", "select='not(eq(mod(n\\,5)\\,2))'", *passthrough, *SAFE_VIDEO,
                *SAFE_MUX)  # fmt: skip


@pytest.fixture(scope="session")
def wide_long(make: Callable[..., str]) -> str:
    """16:9, 60 fps, mono 44.1 kHz, 32 s: needs size, fps, channels and a trim for App Store previews."""
    return make("wide_long.mp4", *src_video("640x360", 60, 32), *src_sine(32, 44100), "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-shortest")  # fmt: skip


# ---------------------------------------------------------------------- check
def test_clean_reel_passes_instagram(clean_reel: str, runner: Runner) -> None:
    report, media = check(clean_reel, "instagram-reels-api", runner)
    assert failing(report) == {"video.size": "info"}  # 360x640 is not the recommended 1080x1920
    assert report.ok
    assert media.boxes is not None and media.boxes.moov_before_mdat
    assert media.packets is not None and not media.packets.reordered
    assert media.loudness is not None and abs((media.loudness.integrated_lufs or 0) + 14) <= 1
    assert media.hiss_lufs is not None and media.hiss_lufs < -60


def test_detects_moov_last(moov_last: str, runner: Runner) -> None:
    report, _ = check(moov_last, "instagram-reels-api", runner)
    assert failing(report) == {"container.faststart": "error", "video.size": "info"}


def test_detects_b_frames_and_edit_lists(b_frames: str, runner: Runner) -> None:
    report, media = check(b_frames, "instagram-reels-api", runner)
    fails = failing(report)
    assert fails["container.edit_list"] == "error"
    assert fails["video.b_frames"] == "warn"
    assert media.encoder_settings and media.encoder_settings["encoder"] == "x264"
    yt, _ = check(b_frames, "youtube-shorts", runner)
    assert failing(yt)["container.edit_list"] == "warn"


def test_detects_open_gop(open_gop: str, runner: Runner) -> None:
    report, media = check(open_gop, "instagram-reels-api", runner)
    assert failing(report)["video.closed_gop"] == "error"
    assert media.packets is not None and media.packets.leading_pictures > 0


def test_detects_vfr(vfr: str, runner: Runner) -> None:
    report, _ = check(vfr, "instagram-reels-api", runner)
    assert failing(report).get("video.cfr") == "warn"


def test_detects_loudness_and_true_peak(loud: str, runner: Runner) -> None:
    report, _ = check(loud, "instagram-reels-api", runner)
    fails = failing(report)
    assert fails["audio.loudness"] == "warn" and fails["audio.true_peak"] == "warn"


def test_hiss_index_separates_clean_and_hissy(clean_reel: str, hissy: str, runner: Runner) -> None:
    clean_report, clean = check(clean_reel, "instagram-reels-api", runner)
    hissy_report, noisy = check(hissy, "instagram-reels-api", runner)
    assert clean.hiss_lufs is not None and noisy.hiss_lufs is not None
    assert noisy.hiss_lufs > -35 > clean.hiss_lufs
    assert "audio.hiss" not in failing(clean_report)
    assert failing(hissy_report)["audio.hiss"] == "warn"


def test_detects_black_and_frozen_openings(black_start: str, frozen_start: str, runner: Runner) -> None:
    report, media = check(black_start, "tiktok", runner)
    assert failing(report)["content.black_start"] == "warn"
    assert "content.frozen_start" not in failing(report)
    assert media.start is not None and 0.4 < media.start.black_start_s < 0.6
    report, media = check(frozen_start, "tiktok", runner)
    assert failing(report)["content.frozen_start"] == "warn"
    assert media.start is not None and media.start.frozen_start_s > 1.2


def test_detects_location_metadata(located: str, runner: Runner) -> None:
    report, media = check(located, "instagram-reels-api", runner)
    assert failing(report)["metadata.location"] == "warn"
    assert media.location_tags


def test_app_store_rules(wide_long: str, runner: Runner) -> None:
    report, _ = check(wide_long, "app-store-preview-iphone", runner)
    fails = failing(report)
    for cid in ("duration", "video.size", "video.fps", "audio.channels"):
        assert fails[cid] == "error", cid


def test_fast_mode_skips_decoding_checks(b_frames: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    media = analyze(b_frames, runner, frozenset())
    report = run_checks(media, prof.rules, file=b_frames, profile=prof.id)
    statuses = {r.id: r.status for r in report.results}
    assert statuses["video.cfr"] == "skip" and statuses["audio.loudness"] == "skip"
    assert statuses["video.b_frames"] == "fail"  # falls back to has_b_frames / x264 settings


def test_rotation_metadata(make: Callable[..., str], runner: Runner, ffmpeg_version: tuple[int, int]) -> None:
    if ffmpeg_version < (6, 0):
        pytest.skip("-display_rotation needs FFmpeg 6")
    base = make("landscape.mp4", *src_video("640x360"), *SAFE_VIDEO, *SAFE_MUX)
    path = os.path.join(os.path.dirname(base), "rotated.mp4")
    try:
        ffmpeg("-display_rotation", "90", "-i", base, "-c", "copy", path)
    except subprocess.CalledProcessError:
        pytest.skip("this FFmpeg build cannot write rotation metadata")
    media = analyze(path, runner, frozenset())
    assert media.video is not None and media.video.rotation in (90, 270)
    assert (media.video.display_width, media.video.display_height) == (360, 640)


# ---------------------------------------------------------------------- hardening
def test_disguised_playlist_and_concat_are_refused(tmp_path: Path, runner: Runner) -> None:
    playlist = tmp_path / "evil.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n")
    concat = tmp_path / "evil.mp4"
    concat.write_text("ffconcat version 1.0\nfile /etc/hosts\n")
    for path in (playlist, concat):
        with pytest.raises(ProbeError):
            analyze(str(path), runner, NEEDS_ALL)


def test_broken_inputs_are_reported(tmp_path: Path, moov_last: str, runner: Runner, capsys) -> None:
    truncated = tmp_path / "truncated.mp4"
    data = Path(moov_last).read_bytes()
    truncated.write_bytes(data[: len(data) // 2])  # moov is at the end, so it is lost
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    for path in (truncated, empty, tmp_path / "missing.mp4"):
        with pytest.raises(ProbeError):
            analyze(str(path), runner, NEEDS_ALL)
    code = main(["check", str(truncated), "-p", "tiktok", "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and doc["results"][0]["error"]


# ---------------------------------------------------------------------- CLI
def test_cli_check_outputs(tmp_path: Path, clean_reel: str, b_frames: str, capsys) -> None:
    folder = tmp_path / "videos"
    folder.mkdir()
    shutil.copy(clean_reel, folder / "a.mp4")
    shutil.copy(b_frames, folder / "b.mp4")
    junit, report_json = tmp_path / "qa.xml", tmp_path / "qa.json"
    code = main(["check", str(folder), "-p", "instagram-reels-api", "--junit", str(junit), "--json", str(report_json),
                 "--color", "never"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1
    assert "a.mp4 · instagram-reels-api · PASS" in out
    assert "b.mp4 · instagram-reels-api · FAIL" in out
    root = ET.parse(junit).getroot()
    assert root.get("failures") == "1"
    doc = json.loads(report_json.read_text())
    assert doc["summary"]["passed"] == 1 and doc["summary"]["failed"] == 1
    assert main(["check", str(folder / "a.mp4"), "-p", "instagram-reels-api", "-q"]) == 0
    assert main(["check", str(folder / "a.mp4"), "-p", "instagram-reels-api", "--strict", "--skip", "video.size"]) == 0
    capsys.readouterr()


def test_cli_probe_and_doctor(clean_reel: str, capsys) -> None:
    assert main(["probe", clean_reel]) == 0
    doc = json.loads(capsys.readouterr().out)[clean_reel]
    assert doc["video"]["codec"] == "h264" and doc["boxes"]["moov_before_mdat"] is True
    assert main(["doctor"]) in (0, 1)
    assert "ffmpeg" in capsys.readouterr().out


def test_doctor_passes_on_a_complete_install(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    """These integration tests need libx264 anyway, so the FFmpeg that runs them must pass every doctor item."""
    monkeypatch.delenv("SVQA_PROFILE_PATH", raising=False)
    items = diagnose()
    assert not [item for item in items if item.failed], render(items)
    assert next(item for item in items if item.name == "pipeline test").status == "ok"


def test_truncated_file_error_names_the_cause_and_the_fix(
    tmp_path: Path, moov_last: str, runner: Runner, capsys
) -> None:
    truncated = tmp_path / "cut.mp4"
    data = Path(moov_last).read_bytes()
    truncated.write_bytes(data[: len(data) // 2])  # the moov box is at the end, so it is lost
    with pytest.raises(ProbeError, match="moov atom not found"):
        analyze(str(truncated), runner, NEEDS_ALL)
    assert main(["check", str(truncated), "-p", "tiktok", "--color", "never"]) == 1
    out = capsys.readouterr().out
    assert "moov atom not found" in out and "hint: The MP4/MOV index (moov box) is missing" in out


# ---------------------------------------------------------------------- fix
def opts(tmp: Path, **kw) -> FixOptions:
    return FixOptions(out_dir=str(tmp), preset="veryfast", **kw)


def test_dry_run_writes_nothing(tmp_path: Path, b_frames: str, runner: Runner) -> None:
    before = sha256_file(b_frames)
    res = fix_file(b_frames, load_profile("instagram-reels-api"), runner, opts(tmp_path))
    assert res.status == "planned" and res.plan is not None and res.plan.mode == "full"
    assert list(tmp_path.iterdir()) == []
    assert sha256_file(b_frames) == before


def test_full_fix_then_idempotent(tmp_path: Path, b_frames: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    before = sha256_file(b_frames)
    res = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True))
    assert res.status == "written", res.message
    assert res.after is not None and res.after.ok and not res.after.failures(("error", "warn"))
    out = Path(res.output or "")
    assert out.exists() and sha256_file(b_frames) == before  # input untouched
    mask = os.umask(0)
    os.umask(mask)
    assert out.stat().st_mode & 0o777 == 0o666 & ~mask  # normal permissions, not mkstemp's 0600
    entry = json.loads((tmp_path / MANIFEST_NAME).read_text())["entries"][out.name]
    assert entry["status"] == "ok" and entry["output_sha256"] == sha256_file(str(out))
    assert entry["source_sha256"] == before and entry["mode"] == "full"
    assert not [p for p in os.listdir(tmp_path) if "svqa-tmp" in p]  # temp files cleaned up
    again = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True))
    assert again.status == "up-to-date"
    report, _ = check(str(out), "instagram-reels-api", runner)
    assert not {k: v for k, v in failing(report).items() if v != "info"}


def test_remux_fix_keeps_video_bits(tmp_path: Path, moov_last: str, runner: Runner) -> None:
    res = fix_file(moov_last, load_profile("instagram-reels-api"), runner, opts(tmp_path, apply=True))
    assert res.status == "written" and res.plan is not None and res.plan.mode == "remux"

    def video_md5(path: str) -> str:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
                             capture_output=True, text=True, check=True).stdout  # fmt: skip
        return out.strip()

    assert video_md5(moov_last) == video_md5(res.output or "")


def test_audio_only_fix(tmp_path: Path, loud: str, runner: Runner) -> None:
    res = fix_file(loud, load_profile("instagram-reels-api"), runner, opts(tmp_path, apply=True))
    assert res.status == "written" and res.plan is not None
    assert (res.plan.mode, res.plan.video_action, res.plan.audio_action) == ("audio", "copy", "encode")
    report, media = check(res.output or "", "instagram-reels-api", runner)
    assert "audio.loudness" not in failing(report) and "audio.true_peak" not in failing(report)
    assert media.loudness is not None and abs((media.loudness.integrated_lufs or 0) + 14) <= 1


def test_metadata_is_stripped(tmp_path: Path, located: str, runner: Runner) -> None:
    res = fix_file(located, load_profile("instagram-reels-api"), runner, opts(tmp_path, apply=True))
    assert res.status == "written"
    _report, media = check(res.output or "", "instagram-reels-api", runner)
    assert media.location_tags == []


def test_app_store_fix_from_landscape_with_trim(tmp_path: Path, wide_long: str, runner: Runner) -> None:
    prof = load_profile("app-store-preview-iphone")
    refused = fix_file(wide_long, prof, runner, opts(tmp_path, apply=True))
    assert refused.status == "refused" and "--trim" in refused.message
    res = fix_file(wide_long, prof, runner, opts(tmp_path, apply=True, trim=True, fit="blur"))
    assert res.status == "written", res.message
    report, media = check(res.output or "", "app-store-preview-iphone", runner)
    assert report.ok, failing(report)
    assert media.video is not None and (media.video.width, media.video.height) == (886, 1920)
    assert media.audio is not None and media.audio.channels == 2 and media.audio.sample_rate == 48000
    assert media.duration_s is not None and 29.5 < media.duration_s <= 30
    assert media.fps is not None and abs(media.fps - 30) < 0.01


def test_refuses_foreign_output_and_in_place(tmp_path: Path, b_frames: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    foreign = tmp_path / "bframes.instagram-reels-api.mp4"
    foreign.write_bytes(b"precious")
    res = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True))
    assert res.status == "refused" and "--force" in res.message
    assert foreign.read_bytes() == b"precious"
    forced = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True, force=True))
    assert forced.status == "written"
    same = fix_file(b_frames, prof, runner, FixOptions(output=b_frames, apply=True))
    assert same.status == "refused"


def test_unfixable_short_clip_is_refused(tmp_path: Path, make: Callable[..., str], runner: Runner) -> None:
    short = make("short.mp4", *src_video(seconds=2), *SAFE_VIDEO, *SAFE_MUX)
    res = fix_file(short, load_profile("instagram-reels-api"), runner, opts(tmp_path, apply=True))
    assert res.status == "refused" and "duration" in res.message
    assert list(tmp_path.iterdir()) == []


def test_cli_fix_dry_run_and_apply(tmp_path: Path, b_frames: str, capsys) -> None:
    out = tmp_path / "fixed.mp4"
    assert main(["fix", b_frames, "-p", "instagram-reels-api", "-o", str(out), "--preset", "veryfast"]) == 0
    text = capsys.readouterr().out
    assert "PLAN (full)" in text and "--apply" in text and not out.exists()
    assert main(["fix", b_frames, "-p", "instagram-reels-api", "-o", str(out), "--preset", "veryfast", "--apply",
                 "--format", "json"]) == 0  # fmt: skip
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"][0]["status"] == "written" and out.exists()


# ---------------------------------------------------------------------- fix: robustness
def temp_files(folder: Path) -> list[str]:
    return [p.name for p in folder.rglob("*") if "svqa-tmp" in p.name or p.name.endswith(".tmp")]


def test_locked_folder_is_a_clean_error(tmp_path: Path, b_frames: str, runner: Runner, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "started_at": "now"}))  # a live run holds it
    res = fix_file(b_frames, load_profile("instagram-reels-api"), runner, opts(out, apply=True))
    assert res.status == "error" and "another `svqa fix` run" in res.message
    assert temp_files(out) == [] and sorted(p.name for p in out.iterdir()) == [LOCK_NAME]
    code = main(["fix", b_frames, "-p", "instagram-reels-api", "-O", str(out), "--apply", "--color", "never"])
    text = capsys.readouterr().out
    assert code == 1 and "ERROR" in text and "1 file: 0 ok, 1 not ok" in text


def test_unreadable_manifest_is_a_clean_error(tmp_path: Path, b_frames: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    (tmp_path / MANIFEST_NAME).write_text("not json{")
    res = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True))
    assert res.status == "error" and "unreadable" in res.message
    assert temp_files(tmp_path) == [] and not (tmp_path / LOCK_NAME).exists()
    # with an existing output, the manifest is read earlier, while checking ownership
    (tmp_path / "bframes.instagram-reels-api.mp4").write_bytes(b"old")
    res = fix_file(b_frames, prof, runner, opts(tmp_path, apply=True))
    assert res.status == "error" and "unreadable" in res.message and res.plan is not None


def test_failed_or_interrupted_rerun_keeps_ownership(
    tmp_path: Path, b_frames: str, moov_last: str, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = load_profile("instagram-reels-api")
    src = tmp_path / "src" / "clip.mp4"
    src.parent.mkdir()
    shutil.copy(b_frames, src)
    out = tmp_path / "out"
    assert fix_file(str(src), prof, runner, opts(out, apply=True)).status == "written"
    shutil.copy(moov_last, src)  # the source changes, so the next run re-encodes
    published = sha256_file(str(out / "clip.instagram-reels-api.mp4"))

    def boom(*_args, **_kwargs):
        raise FixError("simulated encoder failure")

    monkeypatch.setattr(fix_module, "_run", boom)
    res = fix_file(str(src), prof, runner, opts(out, apply=True))
    assert res.status == "error" and "simulated" in res.message
    entry = json.loads((out / MANIFEST_NAME).read_text())["entries"]["clip.instagram-reels-api.mp4"]
    assert entry["status"] == "failed" and entry["output_sha256"] == published

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(fix_module, "_run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        fix_file(str(src), prof, runner, opts(out, apply=True))
    entry = json.loads((out / MANIFEST_NAME).read_text())["entries"]["clip.instagram-reels-api.mp4"]
    assert entry["status"] == "interrupted" and entry["output_sha256"] == published
    assert temp_files(out) == []
    monkeypatch.undo()
    assert fix_file(str(src), prof, runner, opts(out)).status == "planned"  # not "refused": still ours
    assert fix_file(str(src), prof, runner, opts(out, apply=True)).status == "written"


def test_output_of_another_source_needs_force(tmp_path: Path, b_frames: str, open_gop: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    for folder, clip in (("a", b_frames), ("b", open_gop)):
        (tmp_path / folder).mkdir()
        shutil.copy(clip, tmp_path / folder / "clip.mp4")
    out = tmp_path / "up"
    assert fix_file(str(tmp_path / "a" / "clip.mp4"), prof, runner, opts(out, apply=True)).status == "written"
    res = fix_file(str(tmp_path / "b" / "clip.mp4"), prof, runner, opts(out, apply=True))
    assert res.status == "refused" and "another source" in res.message
    forced = fix_file(str(tmp_path / "b" / "clip.mp4"), prof, runner, opts(out, apply=True, force=True))
    assert forced.status == "written"


def test_cli_folder_with_same_names_keeps_both(tmp_path: Path, b_frames: str, open_gop: str, capsys) -> None:
    previews = tmp_path / "previews"
    for folder, clip in (("a", b_frames), ("b", open_gop)):
        (previews / folder).mkdir(parents=True)
        shutil.copy(clip, previews / folder / "clip.mp4")
    up = tmp_path / "upload"
    argv = ["fix", str(previews), "-p", "instagram-reels-api", "-O", str(up), "--apply", "--preset", "veryfast",
            "--format", "json"]  # fmt: skip
    assert main(argv) == 0
    assert [r["status"] for r in json.loads(capsys.readouterr().out)["results"]] == ["written", "written"]
    outputs = [up / "a" / "clip.instagram-reels-api.mp4", up / "b" / "clip.instagram-reels-api.mp4"]
    assert all(p.exists() for p in outputs) and sha256_file(str(outputs[0])) != sha256_file(str(outputs[1]))
    assert main(argv) == 0  # the re-run skips both
    assert [r["status"] for r in json.loads(capsys.readouterr().out)["results"]] == ["up-to-date", "up-to-date"]


def test_strict_is_consistent_across_runs(tmp_path: Path, hissy: str, b_frames: str, runner: Runner) -> None:
    prof = load_profile("instagram-reels-api")
    fresh = fix_file(hissy, prof, runner, opts(tmp_path / "fresh", apply=True, strict=True))
    assert fresh.status == "refused" and "audio.hiss" in fresh.message  # refused before any encoding
    assert not (tmp_path / "fresh").exists()
    out = tmp_path / "out"
    assert fix_file(hissy, prof, runner, opts(out, apply=True, mode="remux")).status == "written"  # hiss only warns
    again = fix_file(hissy, prof, runner, opts(out, apply=True, mode="remux", strict=True))
    assert again.status == "refused" and "audio.hiss" in again.message  # not "up to date"
    # an up-to-date output whose verification warned fails under --strict
    assert fix_file(b_frames, prof, runner, opts(out, apply=True)).status == "written"
    manifest = out / MANIFEST_NAME
    doc = json.loads(manifest.read_text())
    entry = doc["entries"]["bframes.instagram-reels-api.mp4"]
    entry["verification"] = {"errors": 0, "warnings": 1, "warned_checks": ["audio.loudness"]}
    manifest.write_text(json.dumps(doc))
    assert fix_file(b_frames, prof, runner, opts(out, apply=True)).status == "up-to-date"
    strict = fix_file(b_frames, prof, runner, opts(out, apply=True, strict=True))
    assert strict.status == "failed" and "audio.loudness" in strict.message
    skipped = fix_file(b_frames, prof, runner, opts(out, apply=True, strict=True, skip=["audio.loudness"]))
    assert skipped.status == "up-to-date"


def test_anamorphic_source_keeps_its_shape(tmp_path: Path, make: Callable[..., str], runner: Runner) -> None:
    # white 1440x1080 stored with 4:3 pixels: 16:9 on screen
    src = make("anamorphic.mp4", "-f", "lavfi", "-i", "color=c=white:s=1440x1080:r=30:d=2", "-vf", "setsar=4/3",
               *SAFE_VIDEO, *SAFE_MUX)  # fmt: skip
    media = analyze(src, runner, frozenset())
    assert media.video is not None and media.video.sar == "4:3"
    res = fix_file(src, load_profile("tiktok"), runner, opts(tmp_path, apply=True, mode="full"))
    assert res.status == "written", res.message
    out = res.output or ""
    fixed = analyze(out, runner, frozenset())
    assert fixed.video is not None and (fixed.video.width, fixed.video.height) == (1080, 1920)
    assert fixed.video.sar in (None, "1:1")
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-i", out, "-vf", "cropdetect=round=2", "-frames:v", "10"]
    log = subprocess.run([*args, "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    height = int(re.findall(r" h:(\d+) ", log)[-1])  # height of the white picture inside the black bars
    assert abs(height - 608) <= 4  # 1080 / (16/9); the bug made it 810 (4:3)


def test_quicktime_track_handlers(make: Callable[..., str], runner: Runner) -> None:
    mov = make("bframes.mov", *src_video(), *src_sine(), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt",
               "yuv420p", "-c:a", "aac", "-shortest", "-f", "mov")  # fmt: skip
    report, media = check(mov, "instagram-reels-api", runner)
    assert media.boxes is not None and [t.handler for t in media.boxes.tracks] == ["vide", "soun"]
    edit = next(r for r in report.results if r.id == "container.edit_list")
    assert edit.status == "fail" and "video, audio" in edit.message


def test_unsupported_container_is_named(tmp_path: Path, runner: Runner) -> None:
    song = tmp_path / "song.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", str(song))
    with pytest.raises(ProbeError, match=r"unsupported container \(mp3\)"):
        analyze(str(song), runner, frozenset())
