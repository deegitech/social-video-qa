"""Error messages -> one-line fixes, and where the fixes are printed. No FFmpeg needed.

The messages are the ones FFmpeg 8, FFprobe, the OS and svqa really print (captured, with addresses shortened).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from social_video_qa import cli, hints
from social_video_qa.checks import CheckReport, run_checks
from social_video_qa.cli import VIDEO_EXTS, main
from social_video_qa.ffmpeg import FFmpegError, Runner, ffmpeg_reason
from social_video_qa.fix import FixOptions, FixResult, fix_file
from social_video_qa.hints import HINTS, VIDEO_EXTS_TEXT, add_package_command, hint_for, installer, reinstall_command
from social_video_qa.profiles import load_profile
from social_video_qa.report import Style, render_human, render_json, render_junit

from .conftest import make_media

CASES = [
    # the binaries
    ("ffprobe not found. Install FFmpeg (https://ffmpeg.org/download.html) or point SVQA_FFMPEG / SVQA_FFPROBE "
     "at the binaries.", "Run `svqa doctor`"),
    ("ffmpeg and ffprobe not found. Install FFmpeg", "Run `svqa doctor`"),
    ("ffmpeg exited with code -6: dyld[4242]: Library not loaded: /opt/homebrew/opt/x264/lib/libx264.164.dylib",
     "brew reinstall ffmpeg"),
    ("ffmpeg: error while loading shared libraries: libavdevice.so.60: cannot open shared object file",
     "sudo apt install --reinstall ffmpeg"),
    ("ffmpeg does not run: nice: /opt/tools/ffmpeg: Bad CPU type in executable", "built for another processor"),
    ("ffprobe could not be started: [Errno 86] Bad CPU type in executable: '/opt/tools/ffprobe'",
     "Intel-only FFmpeg on Apple silicon"),
    ("ffprobe could not be started: [Errno 13] Permission denied: '/opt/tools/ffprobe'", "does not start"),
    ("ffmpeg does not run: nice: /opt/tools/ffmpeg: Permission denied", "does not start"),
    # what the build lacks
    ("ffmpeg failed: [vost#0:0 @ 0x1] Unknown encoder 'libx264'\n[vost#0:0 @ 0x1] Error selecting an encoder\n"
     "Error opening output files: Encoder not found", "no libx264 encoder (minimal builds and Fedora's ffmpeg-free"),
    ("ffmpeg failed: Unknown encoder 'aac'", "no aac encoder: install a full FFmpeg 4.4+ build"),
    ("ebur128 analysis failed: No such filter: 'ebur128'; Error opening output files: Filter not found",
     "no 'ebur128' filter"),
    ("ffmpeg failed: [in#1 @ 0x1] Unknown input format: 'lavfi'", "no lavfi input"),
    ("ffprobe could not read the file: Unrecognized option 'show_format'.; Error splitting the argument list: "
     "Option not found", "is SVQA_FFPROBE pointing at ffmpeg?"),
    ("start analysis failed: ffprobe exited with code 1: Failed to set value '-hide_banner' for option 'nostdin': "
     "Option not found", "is SVQA_FFMPEG pointing at ffprobe?"),
    ("ffmpeg failed: Unrecognized option 'filter_complex_threads'.", "too old for the option 'filter_complex_threads'"),
    ("ffmpeg failed: Error opening output files: Encoder not found", "lacks an encoder svqa needs"),
    ("ebur128 analysis failed: Error : Filter not found", "lacks a filter svqa needs"),
    ("loudnorm pass 1 printed no measurements", "--no-loudnorm"),
    ("ffprobe returned invalid JSON: Expecting value: line 1 column 1 (char 0)", "same install"),
    # the input file
    ("ffprobe could not read the file: moov atom not found; Invalid data found when processing input",
     "index (moov box) is missing"),
    ("unsupported container (mp3): svqa only opens MP4/MOV/M4V/3GP, Matroska/WebM, AVI and MPEG-TS files",
     "ffmpeg -i INPUT -c copy OUTPUT.mp4"),
    ("ffprobe could not read the file: Invalid data found when processing input", "damaged, not a video"),
    ("ffprobe found no streams", "no audio or video stream"),
    ("file is empty", "0 bytes"),
    ("not a regular file", "devices, pipes and sockets"),
    ("no video files found", VIDEO_EXTS_TEXT),
    # the file system
    ("ffmpeg failed: [out#0/mp4 @ 0x1] Error opening output out/x.mp4: No space left on device", "disk is full"),
    ("cannot open: Permission denied", "write to the output folder"),
    ("[Errno 13] Permission denied: 'upload/.clip.tiktok.mp4.svqa-tmp-ab12.mp4'", "-O DIR"),
    ("cannot open: No such file or directory", "quote names that contain spaces"),
    ("loudness analysis failed: ffmpeg timed out after 600 s", "--timeout 1800"),
    # profiles
    ("unknown profile 'myspace'. Built-in profiles: app-store-preview-ipad, tiktok", "`svqa profiles` lists them"),
    ("profile file not found: ./brand.json", "relative to the current folder"),
    ("p.yaml: YAML profiles need PyYAML (pip install 'social-video-qa[yaml]')", "Add PyYAML to svqa's environment: "),
    ("p.toml: TOML profiles need Python 3.11+, or pip install 'social-video-qa[toml]' on Python 3.10",
     "add tomli to svqa's environment: "),
    ("profile 'typo' (./typo.json) is invalid:\n  - rules.durration: unknown check", "svqa profiles validate FILE"),
    # svqa fix
    ("cannot produce a passing file: audio.hiss: hiss index -25.0 LUFS is above -35 LUFS (cannot be fixed "
     "automatically and --strict treats warnings as failures)", "--skip CHECK"),
    ("cannot produce a passing file: the video cannot fit the size/bitrate limits at a usable quality (cap 180 kbps)",
     "shorten or split it"),
    ("the re-encoded file still fails: video.bitrate", "--keep-failed --show-commands"),
]  # fmt: skip


@pytest.mark.parametrize(("message", "expected"), CASES)
def test_known_messages_get_their_fix(message: str, expected: str) -> None:
    hint = hint_for(message)
    assert hint is not None and expected in hint, hint
    assert "\n" not in hint  # one line


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "another `svqa fix` run is writing to out (out/.svqa-manifest.lock exists). If no other run is active, "
        "delete the lock file.",
        "out/clip.tiktok.mp4 exists and was not written by this tool (or was modified); use --force to replace it",
        "fix needs one profile, not 'all'",
        "something nobody has seen before",
    ],
)
def test_messages_that_already_say_what_to_do_get_no_hint(message: str | None) -> None:
    assert hint_for(message) is None


def test_every_rule_is_reachable() -> None:
    """Each pattern is matched by at least one case above, so no rule is shadowed by an earlier one."""
    used = set()
    for message, _expected in CASES:
        for n, rule in enumerate(HINTS):
            if rule.pattern.search(message):
                used.add(n)
                break
    assert used == set(range(len(HINTS)))


def test_video_extensions_match_the_cli() -> None:
    assert set(VIDEO_EXTS_TEXT.split()) == VIDEO_EXTS


def test_ffmpeg_reason_keeps_the_cause() -> None:
    stderr = (
        "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x97cc2c000] moov atom not found\n"
        "file:clips/a b.mp4: Invalid data found when processing input\n"
    )
    assert ffmpeg_reason(stderr, "clips/a b.mp4") == "moov atom not found; Invalid data found when processing input"
    assert ffmpeg_reason("one\ntwo\nthree\nthree\n") == "two; three"
    assert ffmpeg_reason("") == ""


#: FFmpeg 8's own lines (addresses shortened) when the build lacks a filter, an encoder, or is really ffprobe.
FFMPEG8_NO_FILTER = (
    "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file:clip.mp4':\n"
    "  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, mono, fltp, 69 kb/s (default)\n"
    "[AVFilterGraph @ 0x9b2c4c500] No such filter: 'ebur128'\n"
    "Error opening output file -.\n"
    "Error opening output files: Filter not found\n"
)
FFMPEG8_NO_ENCODER = (
    "[vost#0:0 @ 0x9e704c000] Unknown encoder 'libx264'\n"
    "[vost#0:0 @ 0x9e704c000] Error selecting an encoder\n"
    "Error opening output file -.\n"
    "Error opening output files: Encoder not found\n"
)
FFPROBE_GIVEN_FFMPEG_ARGS = (
    "ffprobe version 8.1.2 Copyright (c) 2007-2030 the FFmpeg developers\n"
    "  built with Apple clang version 21.0.0\n"
    "  configuration: --prefix=/usr/local --enable-gpl --enable-libx264\n"
    "  libavutil      60. 26.102 / 60. 26.102\n"
    "  libswresample   6.  3.102 /  6.  3.102\n"
    "Failed to set value '-hide_banner' for option 'nostdin': Option not found\n"
)


def test_ffmpeg_reason_skips_ffmpeg8_summary_lines() -> None:
    """FFmpeg 7+ ends with two summary lines; the line that names the filter or encoder must survive."""
    assert ffmpeg_reason(FFMPEG8_NO_FILTER) == "No such filter: 'ebur128'; Error opening output files: Filter not found"
    assert ffmpeg_reason(FFMPEG8_NO_ENCODER) == (
        "Unknown encoder 'libx264'; Error opening output files: Encoder not found"
    )
    assert ffmpeg_reason(FFPROBE_GIVEN_FFMPEG_ARGS) == (
        "Failed to set value '-hide_banner' for option 'nostdin': Option not found"
    )
    hint = hint_for("loudness analysis failed: ebur128 analysis failed: " + ffmpeg_reason(FFMPEG8_NO_FILTER))
    assert hint is not None and hint.startswith("This FFmpeg build has no 'ebur128' filter")


def test_ffmpeg_reason_keeps_a_cause_followed_by_other_lines() -> None:
    """Older FFmpeg prints more lines after the cause (constructed log, same shape)."""
    log = (
        "[Parsed_ebur128_0 @ 0x1] No such filter: 'ebur128'\n"
        "Error reinitializing filters!\n"
        "Error while processing the decoded data for stream #0:1\n"
        "Conversion failed!\n"
    )
    assert ffmpeg_reason(log) == "No such filter: 'ebur128'; Conversion failed!"
    assert ffmpeg_reason(log, lines=3) == (
        "No such filter: 'ebur128'; Error while processing the decoded data for stream #0:1; Conversion failed!"
    )
    assert ffmpeg_reason(log, lines=1) == "No such filter: 'ebur128'"


def test_runner_errors_are_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing call with check=True raises a one-line message, so check reports keep their layout."""
    script = tmp_path / "ffprobe"
    script.write_text("#!/bin/sh\nprintf '%s' \"$LOG\" >&2\nexit 1\n")
    script.chmod(0o755)
    runner = Runner(ffmpeg=str(script), ffprobe=str(script), nice=0)
    monkeypatch.setenv("LOG", FFPROBE_GIVEN_FFMPEG_ARGS)
    with pytest.raises(FFmpegError) as exc:
        runner.run([str(script), "-nostdin"])
    assert str(exc.value) == (
        "ffprobe exited with code 1: Failed to set value '-hide_banner' for option 'nostdin': Option not found"
    )


# ---------------------------------------------------------------------- where hints are printed
def broken(name: str = "broken.mp4", profile: str = "tiktok") -> CheckReport:
    return CheckReport(
        file=name,
        profile=profile,
        error="ffprobe could not read the file: moov atom not found; Invalid data found when processing input",
    )


def test_human_report_prints_the_hint_once_per_file() -> None:
    text = render_human([broken()], {}, style=Style())
    assert "hint: The MP4/MOV index (moov box) is missing" in text
    many = render_human([broken(profile=p) for p in ("tiktok", "youtube-shorts", "instagram-reels-api")], {})
    assert many.count("hint: The MP4/MOV index") == 1


def test_a_hint_is_printed_once_per_file() -> None:
    """A missing ebur128 fails four checks (loudness, true peak, hiss, silence): one hint line, not four."""
    m = make_media(path="clip.mp4")
    for name in ("loudness", "hiss"):
        m.measured.discard(name)
        m.analysis_errors[name] = "ebur128 analysis failed: No such filter: 'ebur128'; Error opening output files: ..."
    prof = load_profile("instagram-reels-api")
    rep = run_checks(m, prof.rules, file=m.path, profile=prof.id)
    assert sum(1 for r in rep.results if r.status == "error") >= 3
    text = render_human([rep], {"clip.mp4": m}, style=Style(), verbose=True)
    assert text.count("hint: This FFmpeg build has no 'ebur128' filter") == 1
    other = run_checks(m, load_profile("tiktok").rules, file=m.path, profile="tiktok")
    both = render_human([rep, other], {"clip.mp4": m}, style=Style(), verbose=True)
    assert both.count("hint: This FFmpeg build has no 'ebur128' filter") == 1


def test_failed_analysis_shows_its_cause() -> None:
    m = make_media(path="clip.mp4")
    m.measured.discard("loudness")
    m.analysis_errors["loudness"] = "ebur128 analysis failed: No such filter: 'ebur128'"
    prof = load_profile("instagram-reels-api")
    rep = run_checks(m, prof.rules, file=m.path, profile=prof.id)
    loud = next(r for r in rep.results if r.id == "audio.loudness")
    assert loud.status == "error" and "no 'ebur128' filter" in (loud.hint or "")
    text = render_human([rep], {"clip.mp4": m}, style=Style())
    assert "hint: This FFmpeg build has no 'ebur128' filter" in text
    # ordinary failures keep the check's own fix in JSON, and print no hint line
    m2 = make_media(path="late.mp4")
    m2.boxes.moov_before_mdat = False
    rep2 = run_checks(m2, prof.rules, file=m2.path, profile=prof.id)
    assert "hint:" not in render_human([rep2], {"late.mp4": m2}, style=Style())
    faststart = next(r for r in rep2.results if r.id == "container.faststart")
    assert faststart.hint and "faststart" in faststart.hint


def test_json_and_junit_carry_the_hint() -> None:
    doc = json.loads(render_json([broken()], {}))
    assert doc["results"][0]["hint"].startswith("The MP4/MOV index")
    root = ET.fromstring(render_junit([broken()]))
    error = root.find(".//error")
    assert error is not None and "hint: The MP4/MOV index" in (error.text or "")


def test_fix_results_print_and_carry_the_hint() -> None:
    res = FixResult("a.mp4", "a.tiktok.mp4", "error", "ffmpeg failed: [vost#0:0 @ 0x1] Unknown encoder 'libx264'")
    assert res.to_dict()["hint"].startswith("This FFmpeg build has no libx264 encoder")
    assert FixResult("a.mp4", "a.tiktok.mp4", "planned").to_dict()["hint"] is None


def test_fix_output_shows_the_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")

    def fake(path, profile, runner, opts, *, log=None):
        return FixResult(
            path, path + ".out.mp4", "error", "ffmpeg failed: Error opening output x: No space left on device"
        )

    monkeypatch.setattr(cli, "_runner", lambda args: Runner(ffmpeg="ffmpeg", ffprobe="ffprobe"))
    monkeypatch.setattr(cli, "fix_file", fake)
    assert main(["fix", str(tmp_path / "a.mp4"), "-p", "tiktok", "--color", "never"]) == 1
    assert "  hint: The disk is full" in capsys.readouterr().out


def test_multi_line_fix_errors_stay_indented(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    message = "ffmpeg failed: [vost#0:0 @ 0x1] Unknown encoder 'libx264'\nError opening output files: Encoder not found"

    def fake(path, profile, runner, opts, *, log=None):
        return FixResult(path, path + ".out.mp4", "error", message)

    monkeypatch.setattr(cli, "_runner", lambda args: Runner(ffmpeg="ffmpeg", ffprobe="ffprobe"))
    monkeypatch.setattr(cli, "fix_file", fake)
    assert main(["fix", str(tmp_path / "a.mp4"), "-p", "tiktok", "--color", "never"]) == 1
    out = capsys.readouterr().out.splitlines()
    first = out.index("  ffmpeg failed: [vost#0:0 @ 0x1] Unknown encoder 'libx264'")
    assert out[first + 1] == "    Error opening output files: Encoder not found"
    assert out[first + 2].startswith("  hint: This FFmpeg build has no libx264 encoder")


def test_dry_run_warns_when_the_encoder_is_missing(tmp_path: Path) -> None:
    """Without libx264 (Fedora's ffmpeg-free), the plan says --apply will fail instead of only failing later."""
    m = make_media(path=str(tmp_path / "clip.mp4"))
    runner = Runner(ffmpeg="ffmpeg", ffprobe="ffprobe", nice=0)
    runner._listings["ffmpeg -encoders"] = {"aac", "libopenh264"}  # what `ffmpeg -encoders` lists there
    res = fix_file(m.path, load_profile("instagram-reels-api"), runner, FixOptions(mode="full"), media=m)
    assert res.status == "planned"
    assert res.plan is not None and res.plan.video_action == "encode"
    assert "this FFmpeg has no libx264 encoder: --apply will fail (run svqa doctor)" in res.plan.notes
    runner._listings["ffmpeg -encoders"] = {"aac", "libx264"}
    res = fix_file(m.path, load_profile("instagram-reels-api"), runner, FixOptions(mode="full"), media=m)
    assert res.plan is not None and not any("encoder" in note for note in res.plan.notes)
    runner._listings["ffmpeg -encoders"] = set()  # the list could not be read: say nothing
    res = fix_file(m.path, load_profile("instagram-reels-api"), runner, FixOptions(mode="full"), media=m)
    assert res.plan is not None and not any("encoder" in note for note in res.plan.notes)


@pytest.mark.parametrize(
    ("marker", "kind", "commands"),
    [
        ("pipx_metadata.json", "pipx", ("pipx inject social-video-qa PyYAML", "pipx install --force git+https://")),
        ("uv-receipt.toml", "uv", (" --with PyYAML git+https://", " --reinstall git+https://")),
        (None, "pip", (" -m pip install PyYAML", " -m pip install --force-reinstall ")),
    ],
)
def test_install_commands_match_the_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str | None, kind: str, commands: tuple[str, str]
) -> None:
    """pipx, `uv tool` and venv users each get a command that works for them."""
    add, reinstall = commands
    if marker:
        (tmp_path / marker).write_text("{}")
    monkeypatch.setattr(hints.sys, "prefix", str(tmp_path))
    assert installer() == kind
    assert add in add_package_command("PyYAML") and reinstall in reinstall_command()
    yaml_hint = hint_for("p.yaml: YAML profiles need PyYAML (pip install 'social-video-qa[yaml]')")
    assert yaml_hint is not None and add in yaml_hint
    if kind == "pip":
        assert add_package_command("tomli").startswith(hints.shlex.quote(hints.sys.executable))
    if kind == "uv":  # the Python svqa runs on now, so an old system Python is not picked up again
        version = f"{hints.sys.version_info.major}.{hints.sys.version_info.minor}"
        assert add_package_command("PyYAML").startswith(f"uv tool install --python {version} --with PyYAML")


def test_command_errors_print_the_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "_runner", lambda args: Runner(ffmpeg="ffmpeg", ffprobe="ffprobe"))
    (tmp_path / "notes.txt").write_text("no videos here")
    assert main(["check", str(tmp_path), "-p", "tiktok"]) == 2
    err = capsys.readouterr().err
    assert "svqa: no video files found\nsvqa: hint: svqa looks for .mp4" in err
    assert main(["check", str(tmp_path), "-p", "myspace"]) == 2
    assert "svqa: hint: `svqa profiles` lists them" in capsys.readouterr().err
