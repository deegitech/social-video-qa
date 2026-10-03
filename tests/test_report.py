"""Report rendering: human, JSON, JUnit and profile Markdown (no FFmpeg)."""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET

from social_video_qa.checks import REGISTRY, CheckReport, run_checks
from social_video_qa.profiles import load_profile
from social_video_qa.report import (
    Style,
    fix_command,
    media_summary,
    render_human,
    render_json,
    render_junit,
    render_profile_markdown,
    render_profile_text,
    summary_line,
)

from .conftest import make_media


def reports(strict: bool = False) -> tuple[list[CheckReport], dict]:
    good = make_media(path="good.mp4")
    bad = make_media(path="bad.mp4")
    bad.boxes.moov_before_mdat = False
    bad.packets.reordered = True
    bad.hiss_lufs = -25.0
    prof = load_profile("instagram-reels-api")
    out = [
        run_checks(m, prof.rules, file=m.path, profile=prof.id, profile_title=prof.title, strict=strict)
        for m in (good, bad)
    ]
    out.append(CheckReport(file="broken.mp4", profile=prof.id, error="ffprobe could not read the file", strict=strict))
    return out, {"good.mp4": good, "bad.mp4": bad}


def test_human_report_plain() -> None:
    reps, media = reports()
    text = render_human(reps, media, style=Style(color=False, unicode=False))
    assert "good.mp4 · instagram-reels-api · PASS" in text
    assert "bad.mp4 · instagram-reels-api · FAIL (1 error, 2 warnings)" in text
    assert "FAIL container.faststart" in text
    assert "WARN video.b_frames" in text
    assert "WARN audio.hiss" in text
    assert "note: Observed 2026-10" in text
    assert "fix: svqa fix bad.mp4 -p instagram-reels-api" in text
    assert "ERROR" in text and "ffprobe could not read the file" in text
    assert "\033[" not in text
    assert text.rstrip().endswith("1 error, 2 warnings") is False  # summary counts the broken file too
    assert "3 files x 1 profile: 1 passed (0 with warnings), 2 failed" in text


def test_human_report_verbose_quiet_and_color() -> None:
    reps, media = reports()
    verbose = render_human(reps, media, style=Style(color=False, unicode=True), verbose=True)
    assert "✓ duration" in verbose
    quiet = render_human(reps, media, style=Style(color=False), quiet=True)
    assert "good.mp4" not in quiet
    colored = render_human(reps, media, style=Style(color=True))
    assert "\033[31" in colored


def test_no_fix_hint_when_nothing_is_fixable() -> None:
    m = make_media(path="short.mp4", duration_s=1.0)
    prof = load_profile("instagram-reels-api")
    rep = run_checks(m, prof.rules, file=m.path, profile=prof.id)
    assert "svqa fix" not in render_human([rep], {"short.mp4": m}, style=Style())


def test_fix_hint_names_the_profile_as_the_user_did() -> None:
    m = make_media(path="my clip.mp4")
    m.boxes.moov_before_mdat = False
    prof = load_profile("instagram-reels-api")
    rep = run_checks(m, prof.rules, file=m.path, profile="brand-reels")
    assert fix_command(rep) == "svqa fix 'my clip.mp4' -p brand-reels"
    rep.profile_ref = "./profiles/brand-reels.json"
    assert fix_command(rep) == "svqa fix 'my clip.mp4' -p ./profiles/brand-reels.json"
    rep.profile_ref = "brand-reels"
    text = render_human([rep], {m.path: m}, style=Style(), profile_dirs=["house profiles"])
    assert "fix: svqa fix 'my clip.mp4' -p brand-reels --profile-dir 'house profiles'" in text


def test_matrix_view_for_several_profiles() -> None:
    m = make_media(path="clip.mp4")
    reps = [
        run_checks(m, load_profile(pid).rules, file="clip.mp4", profile=pid)
        for pid in ("instagram-reels-api", "app-store-preview-iphone")
    ]
    text = render_human(reps, {"clip.mp4": m}, style=Style(unicode=False))
    assert "PASS instagram-reels-api" in text
    assert "FAIL app-store-preview-iphone" in text
    assert "duration" in text and "video.size" in text


def test_media_summary_lines() -> None:
    rows = dict(media_summary(make_media()))
    assert rows["video"].startswith("h264 High@4.0 · 1080x1920 · 30 fps CFR")
    assert "-14 LUFS" in rows["audio"]
    assert "moov first" in rows["file"] and "no edit lists" in rows["file"] and "9:16" in rows["file"]


def test_json_report() -> None:
    reps, media = reports()
    doc = json.loads(render_json(reps, media, ffmpeg_version="7.1"))
    assert doc["schema"] == "social-video-qa/report@1"
    assert doc["ok"] is False
    assert doc["ffmpeg"]["version"] == "7.1"
    assert doc["summary"] == {
        "files": 3,
        "profiles": ["instagram-reels-api"],
        "passed": 1,
        "failed": 2,
        "errors": 2,
        "warnings": 2,
    }
    bad = next(r for r in doc["results"] if r["file"] == "bad.mp4")
    faststart = next(c for c in bad["checks"] if c["id"] == "container.faststart")
    assert faststart["status"] == "fail" and faststart["level"] == "error" and faststart["hint"]
    assert "bad.mp4" in doc["media"] and doc["media"]["bad.mp4"]["boxes"]["moov_before_mdat"] is False


def test_junit_report_is_valid_xml() -> None:
    reps, _media = reports()
    root = ET.fromstring(render_junit(reps))
    assert root.tag == "testsuites"
    assert root.get("failures") == "1"
    assert root.get("errors") == "1"
    suites = root.findall("testsuite")
    assert len(suites) == 3
    bad = next(s for s in suites if s.get("name", "").startswith("bad.mp4"))
    failure = bad.find("testcase[@name='container.faststart']/failure")
    assert failure is not None and "moov" in (failure.get("message") or "")
    warn_case = bad.find("testcase[@name='video.b_frames']/system-out")
    assert warn_case is not None and (warn_case.text or "").startswith("WARN")


def test_junit_strict_turns_warnings_into_failures() -> None:
    reps, _media = reports(strict=True)
    root = ET.fromstring(render_junit(reps, strict=True))
    assert root.get("failures") == "3"


def test_summary_line_counts() -> None:
    reps, _media = reports()
    assert summary_line(reps).startswith("3 files x 1 profile: 1 passed")


def test_profile_rendering() -> None:
    prof = load_profile("meta-ads-instagram")
    text = render_profile_text(prof)
    assert "duration" in text and "<= 15 s" in text and "[observed]" in text
    assert "source: Meta Business Help" in text
    md = render_profile_markdown(prof)
    assert md.startswith("| Check | Requirement | Severity | Basis |")
    assert "| `duration` | <= 15 s<br><sub>Observed 2026-10" in md and "| warn | observed |" in md


def _severities(profile_id: str) -> dict[str, str]:
    """Check ID -> severity column of `svqa profiles show`."""
    out = {}
    for line in render_profile_text(load_profile(profile_id)).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in REGISTRY:
            out[parts[0]] = parts[1]
    return out


def test_rules_with_only_recommendations_show_warn() -> None:
    apple = _severities("app-store-preview-iphone")
    assert apple["bitrate.total"] == "warn"  # only recommended_* values: it can never fail above warn
    assert apple["duration"] == "error" and apple["video.size"] == "error"
    meta = _severities("meta-ads-instagram")
    assert meta["video.aspect"] == "warn" and meta["video.codec"] == "warn" and meta["duration"] == "warn"
    assert meta["file.size"] == "error" and meta["video.chroma"] == "warn"
    assert "| `bitrate.total` | recommended 9800-12300 kbps" in render_profile_markdown(
        load_profile("app-store-preview-iphone")
    )


def test_style_for_stream_respects_no_color(monkeypatch) -> None:
    class Tty(io.StringIO):
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert Style.for_stream(Tty()).color
    monkeypatch.setenv("NO_COLOR", "1")
    assert not Style.for_stream(Tty()).color
    assert Style.for_stream(Tty(), "always").color
    assert not Style.for_stream(io.StringIO(), "auto").color
