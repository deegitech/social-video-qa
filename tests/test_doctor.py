"""`svqa doctor` against a fake FFmpeg: every item, the fix it prints, and the exit code. No FFmpeg needed."""

from __future__ import annotations

import functools
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from social_video_qa import doctor
from social_video_qa.cli import EXIT_FAIL, EXIT_OK, EXIT_USAGE, main
from social_video_qa.doctor import CHECK_FILTERS, ENCODERS, FIX_FILTERS, Item, diagnose, install_lines, render
from social_video_qa.ffmpeg import Runner, ToolNotFound, listed_names, program_name
from social_video_qa.report import Style

ALL_FILTERS = (*CHECK_FILTERS, *FIX_FILTERS, "asplit", "volume")
ALL_DEMUXERS = ("avi", "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "mpegts", "mp3")

ORDER = [
    "python",
    "ffmpeg",
    "ffprobe",
    "ffmpeg version",
    "ffprobe version",
    "demuxers",
    "check filters",
    "fix filters",
    "encoders",
    "lavfi input",
    "pipeline test",
    "built-in profiles",
    "profile folders",
    "profile formats",
]


# ---------------------------------------------------------------------- listings as FFmpeg 8 prints them
def filters_listing(names: Iterable[str]) -> str:
    head = "Filters:\n  T.. = Timeline support\n  .S. = Slice threading\n  A = Audio input/output\n"
    head += "  | = Source or sink filter\n  ------\n"
    return head + "".join(f" .. {n:<16} A->A       Some filter.\n" for n in names)


def encoders_listing(names: Iterable[str]) -> str:
    head = "Encoders:\n V..... = Video\n A..... = Audio\n .F.... = Frame-level multithreading\n ------\n"
    return head + "".join(f" V....D {n:<20} Some encoder\n" for n in names)


def devices_listing(names: Iterable[str]) -> str:
    head = "Devices:\n D. = Demuxing supported\n .E = Muxing supported\n ---\n"
    return head + "".join(f" D  {n:<15} Some device\n" for n in names)


def demuxers_listing(names: Iterable[str]) -> str:
    head = "Formats:\n D.. = Demuxing supported\n .E. = Muxing supported\n ..d = Is a device\n ---\n"
    rows = "".join(f" D   {n:<15} Some format\n" for n in names)
    return head + rows + " D d lavfi           Libavfilter virtual input device\n"


class FakeFFmpeg:
    """Answers the commands doctor runs, like a healthy FFmpeg 8.1 unless told otherwise."""

    def __init__(
        self,
        *,
        version: str = "8.1.2",
        probe_version: str | None = None,
        version_error: str | None = None,
        filters: Iterable[str] = ALL_FILTERS,
        encoders: Iterable[str] = (*ENCODERS, "libx264rgb"),
        devices: Iterable[str] = ("lavfi",),
        demuxers: Iterable[str] = ALL_DEMUXERS,
        pipeline: tuple[int, str] = (0, ""),
        version_text: str | None = None,
    ) -> None:
        self.version, self.probe_version, self.version_error = version, probe_version or version, version_error
        self.version_text = version_text
        self.listings = {
            "-filters": filters_listing(filters),
            "-encoders": encoders_listing(encoders),
            "-devices": devices_listing(devices),
            "-demuxers": demuxers_listing(demuxers),
        }
        self.pipeline = pipeline
        self.calls: list[list[str]] = []

    def run(self, args: list[str], *, timeout: object = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        binary = os.path.basename(args[0])
        if "-version" in args:
            if self.version_error and binary == "ffmpeg":
                return subprocess.CompletedProcess(args, 134, "", self.version_error)
            number = self.version if binary == "ffmpeg" else self.probe_version
            text = f"{binary} version {number} Copyright (c) 2000-2030 the FFmpeg developers\nbuilt with clang\n"
            return subprocess.CompletedProcess(args, 0, self.version_text or text, "")
        for flag, text in self.listings.items():
            if flag in args:
                return subprocess.CompletedProcess(args, 0, text, "")
        code, stderr = self.pipeline
        return subprocess.CompletedProcess(args, code, "", stderr)

    def factory(self) -> Callable[[str, str], Runner]:
        def make(ffmpeg: str, ffprobe: str) -> Runner:
            runner = Runner(ffmpeg=ffmpeg, ffprobe=ffprobe, nice=0)
            runner.run = self.run  # type: ignore[method-assign]
            return runner

        return make


@pytest.fixture
def binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Executable stand-ins for ffmpeg/ffprobe (never run), named by SVQA_FFMPEG / SVQA_FFPROBE."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        path = folder / name
        path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
    monkeypatch.setenv("SVQA_FFMPEG", str(folder / "ffmpeg"))
    monkeypatch.setenv("SVQA_FFPROBE", str(folder / "ffprobe"))
    monkeypatch.delenv("SVQA_PROFILE_PATH", raising=False)
    return folder


def by_name(items: list[Item]) -> dict[str, Item]:
    return {item.name: item for item in items}


def run_doctor(fake: FakeFFmpeg, *dirs: str, platform: str = "linux") -> list[Item]:
    return diagnose(dirs, platform=platform, make_runner=fake.factory())


def text_of(items: list[Item], unicode: bool = True) -> str:
    return render(items, Style(color=False, unicode=unicode))


# ---------------------------------------------------------------------- healthy
def test_healthy_install_is_ready(binaries: Path) -> None:
    fake = FakeFFmpeg()
    items = run_doctor(fake)
    assert [i.name for i in items] == ORDER
    assert all(i.status in ("ok", "skip") for i in items)
    assert by_name(items)["profile folders"].status == "skip"
    out = text_of(items)
    assert "  ✓ ffmpeg version     8.1.2 (4.4 or newer needed)" in out
    assert "8.1.2 (same build as ffmpeg)" in out
    assert "fix:" not in out and "ready: svqa check and svqa fix can use every feature." in out
    # the pipeline test encodes synthetic sources into the null muxer: nothing is written anywhere
    pipeline = fake.calls[-1]
    assert pipeline[-3:] == ["-f", "null", "-"] and "libx264" in pipeline and "lavfi" in pipeline
    assert not any(arg.startswith("file:") for arg in pipeline)


def test_listings_are_read_once(binaries: Path) -> None:
    fake = FakeFFmpeg()
    run_doctor(fake)
    flags = [c[-1] for c in fake.calls if c[-1].startswith("-") and c[-1] != "-"]
    assert sorted(flags) == ["-demuxers", "-devices", "-encoders", "-filters", "-version", "-version"]


def test_git_builds_count_as_recent(binaries: Path) -> None:
    items = by_name(run_doctor(FakeFFmpeg(version="N-121000-g0123abcd")))
    assert items["ffmpeg version"].status == "ok" and "git build" in items["ffmpeg version"].detail


# ---------------------------------------------------------------------- binaries
def test_missing_binaries_get_install_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for var in ("SVQA_FFMPEG", "SVQA_FFPROBE", "SVQA_PROFILE_PATH"):
        monkeypatch.delenv(var, raising=False)
    items = diagnose((), platform="darwin")
    found = by_name(items)
    assert found["ffmpeg"].status == "fail" and found["ffmpeg"].detail == "not found on PATH"
    assert found["ffmpeg"].fix[0].startswith("brew install ffmpeg")
    assert "export SVQA_FFPROBE=/path/to/ffprobe" in found["ffprobe"].fix[-1]
    assert found["FFmpeg features"].status == "skip"
    # what does not need FFmpeg is still checked
    assert found["built-in profiles"].status == "ok"
    linux = by_name(diagnose((), platform="linux"))
    assert any("sudo apt install ffmpeg" in line for line in linux["ffmpeg"].fix)
    assert any("RPM Fusion" in line for line in linux["ffmpeg"].fix)


def test_bad_override_names_the_variable(binaries: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVQA_FFMPEG", str(binaries / "nope"))
    item = by_name(run_doctor(FakeFFmpeg()))["ffmpeg"]
    assert item.status == "fail" and f"SVQA_FFMPEG is set to {binaries / 'nope'}" in item.detail
    assert item.fix == [
        "point it at the ffmpeg binary: export SVQA_FFMPEG=/path/to/ffmpeg",
        "or use PATH: unset SVQA_FFMPEG",
    ]


def test_old_ffmpeg_fails(binaries: Path) -> None:
    item = by_name(run_doctor(FakeFFmpeg(version="4.2.7")))["ffmpeg version"]
    assert item.status == "fail" and item.detail == "4.2.7 is older than 4.4"
    assert item.fix[0] == "install a newer FFmpeg:" and any("static build" in line for line in item.fix)
    assert item.fix[-1].startswith("if another ffmpeg comes first on PATH")
    # an old Homebrew FFmpeg needs an upgrade: `brew install` would only answer "already installed"
    mac = by_name(run_doctor(FakeFFmpeg(version="4.2.7"), platform="darwin"))["ffmpeg version"]
    assert mac.fix[1].startswith("brew upgrade ffmpeg   (or brew install ffmpeg if it did not come from Homebrew")


def test_ffmpeg_that_does_not_start(binaries: Path) -> None:
    error = "dyld[4242]: Library not loaded: /opt/homebrew/opt/x264/lib/libx264.164.dylib"
    items = by_name(run_doctor(FakeFFmpeg(version_error=error), platform="darwin"))
    assert items["ffmpeg version"].status == "fail"
    assert "brew reinstall ffmpeg" in items["ffmpeg version"].fix[0]
    assert items["FFmpeg features"].status == "skip" and "pipeline test" not in items


def test_ffprobe_from_another_install_only_warns(binaries: Path) -> None:
    items = run_doctor(FakeFFmpeg(probe_version="6.1.1"))
    probe = by_name(items)["ffprobe version"]
    assert probe.status == "warn" and probe.detail == "6.1.1, but ffmpeg is 8.1.2: two different installs"
    out = text_of(items)
    assert "ready" in out and "(see the ! above)" in out


def test_svqa_ffprobe_pointing_at_ffmpeg_is_caught(binaries: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this, doctor said "ready" and every `svqa check` failed with "Unrecognized option 'show_format'"."""
    monkeypatch.setenv("SVQA_FFPROBE", str(binaries / "ffmpeg"))
    items = run_doctor(FakeFFmpeg())
    found = by_name(items)
    assert found["ffmpeg version"].status == "ok"
    probe = found["ffprobe version"]
    assert probe.status == "fail"
    assert probe.detail == f"SVQA_FFPROBE points at ffmpeg ({binaries / 'ffmpeg'}), not ffprobe"
    assert probe.fix == [f"export SVQA_FFPROBE={binaries / 'ffprobe'}"]  # the real one, found next to it
    assert found["FFmpeg features"].status == "skip" and "pipeline test" not in found
    assert "not ready: 1 problem" in text_of(items)


def test_svqa_ffmpeg_pointing_at_ffprobe_is_caught(binaries: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this, the pipeline test failed with "for option 'nostdin'" and advised reinstalling FFmpeg."""
    monkeypatch.setenv("SVQA_FFMPEG", str(binaries / "ffprobe"))
    found = by_name(run_doctor(FakeFFmpeg()))
    item = found["ffmpeg version"]
    assert item.status == "fail"
    assert item.detail == f"SVQA_FFMPEG points at ffprobe ({binaries / 'ffprobe'}), not ffmpeg"
    assert item.fix == [f"export SVQA_FFMPEG={binaries / 'ffmpeg'}"]
    assert found["ffprobe version"].status == "ok" and "same build" not in found["ffprobe version"].detail


def test_wrong_program_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An `ffprobe` on PATH that is really ffmpeg (a bad link): no real ffprobe next to it to suggest."""
    folder = tmp_path / "bin"
    folder.mkdir()
    real = folder / "ffmpeg"
    real.write_text("#!/bin/sh\nexit 1\n")
    real.chmod(0o755)
    other = tmp_path / "other"
    other.mkdir()
    (other / "ffprobe").symlink_to(real)
    monkeypatch.setenv("PATH", os.pathsep.join([str(folder), str(other)]))
    for var in ("SVQA_FFMPEG", "SVQA_FFPROBE", "SVQA_PROFILE_PATH"):
        monkeypatch.delenv(var, raising=False)
    fake = FakeFFmpeg(version_text="ffmpeg version 8.1.2 Copyright (c) 2000-2030 the FFmpeg developers\n")
    item = by_name(diagnose((), platform="linux", make_runner=fake.factory()))["ffprobe version"]
    assert item.status == "fail" and item.detail == f"the ffprobe on PATH ({other / 'ffprobe'}) is really ffmpeg"
    assert item.fix[0] == "point SVQA_FFPROBE at the real ffprobe: export SVQA_FFPROBE=/path/to/ffprobe"
    assert item.fix[1].startswith("or reinstall FFmpeg (Debian/Ubuntu: sudo apt install --reinstall ffmpeg")


def test_one_file_for_both_binaries_fails(binaries: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both variables name one file whose -version output does not say which program it is."""
    monkeypatch.setenv("SVQA_FFPROBE", str(binaries / "ffmpeg"))
    found = by_name(run_doctor(FakeFFmpeg(version_text="a wrapper script\n")))
    item = found["ffprobe version"]
    assert item.status == "fail" and item.detail == f"ffmpeg and ffprobe are the same file ({binaries / 'ffmpeg'})"
    assert found["FFmpeg features"].status == "skip"


def test_program_name() -> None:
    assert program_name("ffprobe version 8.1.2 Copyright (c) 2007-2030 the FFmpeg developers\nbuilt with clang") == (
        "ffprobe"
    )
    assert program_name("ffmpeg version N-121000-g0123abcd Copyright (c) 2000-2030") == "ffmpeg"
    assert program_name("ffplay version 7.1 Copyright") == "ffplay"
    assert program_name("") is None and program_name("version 8.1") is None


# ---------------------------------------------------------------------- what the build lacks
def test_missing_encoder_and_filter(binaries: Path) -> None:
    fake = FakeFFmpeg(encoders=["aac"], filters=[f for f in ALL_FILTERS if f != "loudnorm"])
    items = by_name(run_doctor(fake, platform="darwin"))
    assert items["encoders"].status == "fail"
    assert items["encoders"].detail == "missing: libx264 (needed to re-encode video)"
    assert items["encoders"].fix[:2] == [
        "install a full FFmpeg build:",
        "brew install ffmpeg   (Homebrew: https://brew.sh)",
    ]
    assert items["fix filters"].status == "fail" and "loudnorm" in items["fix filters"].detail
    assert items["fix filters"].fix[-1] == "until then, svqa fix --no-loudnorm leaves loudness alone"
    assert items["pipeline test"].status == "skip"


def test_missing_analysis_filter_suggests_fast(binaries: Path) -> None:
    items = by_name(run_doctor(FakeFFmpeg(filters=[f for f in ALL_FILTERS if f != "ebur128"])))
    item = items["check filters"]
    assert item.status == "fail" and "cannot measure loudness, true peak, silence and hiss" in item.detail
    assert item.fix[-1] == "until then, svqa check --fast skips the analyses that need them"


def test_missing_lavfi_and_demuxer(binaries: Path) -> None:
    items = by_name(run_doctor(FakeFFmpeg(devices=["avfoundation"], demuxers=["avi", "mov,mp4"])))
    assert items["lavfi input"].status == "fail" and "silent track" in items["lavfi input"].detail
    assert items["demuxers"].status == "fail"
    assert items["demuxers"].detail == "missing: matroska, mpegts (svqa cannot open MKV/WebM, MPEG-TS)"


def test_failed_pipeline_test_gets_the_hint(binaries: Path) -> None:
    stderr = (  # what FFmpeg 8 prints when the encoder is missing
        "[vost#0:0 @ 0x1234abcd] Unknown encoder 'libx264'\n"
        "[vost#0:0 @ 0x1234abcd] Error selecting an encoder\n"
        "Error opening output file -.\n"
        "Error opening output files: Encoder not found\n"
    )
    item = by_name(run_doctor(FakeFFmpeg(pipeline=(8, stderr))))["pipeline test"]
    assert item.status == "fail"
    assert (
        item.detail
        == "a 0.5 s test encode failed: Unknown encoder 'libx264'; Error opening output files: Encoder not found"
    )
    assert item.fix[0].startswith("This FFmpeg build has no libx264 encoder")


# ---------------------------------------------------------------------- profiles
def test_profile_folders_are_validated(binaries: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "house.json").write_text(json.dumps({"extends": "tiktok", "rules": {"duration": {"max_s": 60}}}))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "typo.json").write_text(json.dumps({"rules": {"durration": {}}}))
    (bad / "notes.txt").write_text("not a profile")
    monkeypatch.setenv("SVQA_PROFILE_PATH", os.pathsep.join([str(bad), str(tmp_path / "missing")]))
    items = [i for i in run_doctor(FakeFFmpeg(), str(good)) if i.name == "profile folder"]
    assert [i.status for i in items] == ["ok", "fail", "fail"]
    assert items[0].detail == f"{good}: 1 profile(s), all valid"
    assert items[1].detail == f"{bad}: 1 of 1 invalid (typo.json)"
    assert items[1].fix[0] == "typo.json: rules.durration: unknown check"
    validate = shlex.join(["svqa", "profiles", "validate", str(bad / "typo.json"), "--profile-dir", str(good)])
    assert items[1].fix[-1] == f"see every problem: {validate}"
    assert items[2].detail == f"{tmp_path / 'missing'} does not exist (from SVQA_PROFILE_PATH)"


def test_every_invalid_profile_is_in_the_validate_command(binaries: Path, tmp_path: Path) -> None:
    folder = tmp_path / "my profiles"
    folder.mkdir()
    (folder / "broken.json").write_text("{not json")
    (folder / "typo.json").write_text(json.dumps({"rules": {"durration": {}}}))
    item = next(i for i in run_doctor(FakeFFmpeg(), str(folder)) if i.name == "profile folder")
    assert item.detail == f"{folder}: 2 of 2 invalid (broken.json, typo.json)"
    files = [str(folder / "broken.json"), str(folder / "typo.json")]
    assert item.fix[-1] == "see every problem: " + shlex.join(
        ["svqa", "profiles", "validate", *files, "--profile-dir", str(folder)]
    )
    assert "'" in item.fix[-1]  # the space in the folder name is quoted


# ---------------------------------------------------------------------- output and exit codes
def test_ascii_output_and_fix_alignment(binaries: Path) -> None:
    items = run_doctor(FakeFFmpeg(encoders=["aac"]))
    out = text_of(items, unicode=False)
    assert "✓" not in out and "✗" not in out
    lines = out.splitlines()
    row = next(n for n, line in enumerate(lines) if "FAIL encoders" in line)
    detail_col = lines[row].index("missing:")
    assert lines[row + 1].index("fix: ") == detail_col
    assert lines[row + 2].index("Debian/Ubuntu") == detail_col + len("fix: ")
    assert "not ready: 1 problem. Apply the fix under each FAIL, then run svqa doctor again." in out


def test_cli_exit_codes(binaries: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real = doctor.diagnose
    monkeypatch.setattr(doctor, "diagnose", functools.partial(real, make_runner=FakeFFmpeg().factory()))
    assert main(["doctor", "--color", "never"]) == EXIT_OK
    assert "ready" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "diagnose", functools.partial(real, make_runner=FakeFFmpeg(version="4.3.1").factory()))
    assert main(["doctor", "--color", "never"]) == EXIT_FAIL
    out = capsys.readouterr().out
    assert "4.3.1 is older than 4.4" in out and "troubleshooting.md" in out


def test_doctor_has_no_timeout_option(capsys: pytest.CaptureFixture[str]) -> None:
    """doctor runs no analysis steps, so it offers no --timeout that would do nothing."""
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--timeout", "5"])
    assert exc.value.code == EXIT_USAGE and "unrecognized arguments: --timeout" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["doctor", "--help"])
    help_text = capsys.readouterr().out
    assert "--threads" in help_text and "--timeout" not in help_text


def test_never_prints_secrets(
    binaries: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = "not-a-real-value-0123456789"
    for var in ("META_ACCESS_TOKEN", "GOOGLE_ADS_REFRESH_TOKEN", "ASC_PRIVATE_KEY", "SVQA_TOKEN"):
        monkeypatch.setenv(var, planted)
    monkeypatch.setattr(doctor, "diagnose", functools.partial(doctor.diagnose, make_runner=FakeFFmpeg().factory()))
    main(["doctor", "--color", "never"])
    captured = capsys.readouterr()
    assert planted not in captured.out and planted not in captured.err


def test_a_binary_that_cannot_start_raises_tool_not_found(tmp_path: Path) -> None:
    program = tmp_path / "ffmpeg"
    program.write_text("not a program")
    program.chmod(0o644)
    runner = Runner(ffmpeg=str(program), ffprobe=str(program), nice=0)
    with pytest.raises(ToolNotFound, match="could not be started"):
        runner.run([str(program), "-version"])


def test_install_lines_per_platform() -> None:
    assert install_lines("darwin") == ["brew install ffmpeg   (Homebrew: https://brew.sh)"]
    assert len(install_lines("linux")) == 3
    assert "ffmpeg.org/download.html" in install_lines("win32")[0]


def test_listed_names_parses_real_listings() -> None:
    demuxers = listed_names(demuxers_listing(ALL_DEMUXERS))
    assert {"mov", "mp4", "matroska", "webm", "avi", "mpegts", "lavfi"} <= demuxers
    assert "d" not in demuxers and "=" not in demuxers and "D.." not in demuxers
    assert listed_names(filters_listing(["ebur128"])) == {"ebur128"}
    assert listed_names(encoders_listing(["libx264"])) == {"libx264"}
    assert listed_names(devices_listing(["lavfi"])) == {"lavfi"}
    assert listed_names("") == set()
