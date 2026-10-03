"""CLI behaviour that does not need FFmpeg: argument errors, profiles commands, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_video_qa import __version__, cli
from social_video_qa.cli import EXIT_ENV, EXIT_FAIL, EXIT_OK, EXIT_USAGE, collect_files, collect_inputs, main
from social_video_qa.ffmpeg import Runner
from social_video_qa.fix import FixError, FixResult, output_for


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys)
    assert code == EXIT_USAGE
    assert "check" in out and "fix" in out


def test_bad_arguments_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["check"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["fix", "a.mp4"])  # --profile is required
    assert exc.value.code == 2


def test_profiles_list_show_validate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = run(capsys, "profiles")
    assert code == EXIT_OK and "instagram-reels-api" in out and "tiktok" in out
    code, out, _ = run(capsys, "profiles", "list", "--format", "json")
    assert code == EXIT_OK and {p["id"] for p in json.loads(out)} >= {"youtube-shorts"}
    code, out, _ = run(capsys, "profiles", "show", "app-store-preview-ipad")
    assert code == EXIT_OK and "1200x1600" in out and "extends: app-store-preview-iphone" in out
    code, out, _ = run(capsys, "profiles", "show", "tiktok", "--format", "json")
    assert json.loads(out)["rules"]["duration"]["max_s"] == 600
    code, out, _ = run(capsys, "profiles", "show", "tiktok", "--format", "markdown")
    assert out.startswith("| Check |")
    code, _out, err = run(capsys, "profiles", "show", "nope")
    assert code == EXIT_USAGE and "unknown profile" in err

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"extends": "tiktok", "rules": {"duration": {"max_s": 60}}}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"rules": {"durration": {}}}), encoding="utf-8")
    code, out, _ = run(capsys, "profiles", "validate", str(good), str(bad))
    assert code == EXIT_USAGE
    assert f"ok       {good}" in out and f"invalid  {bad}" in out and "durration" in out


def test_profile_dir_option(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "house.json").write_text(json.dumps({"title": "House", "rules": {}}), encoding="utf-8")
    code, out, _ = run(capsys, "profiles", "list", "--profile-dir", str(tmp_path))
    assert code == EXIT_OK and "house" in out and str(tmp_path) in out


def test_missing_ffmpeg_exits_3(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SVQA_FFPROBE", str(tmp_path / "no-such-ffprobe"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 16)
    code, _out, err = run(capsys, "check", str(clip), "-p", "tiktok")
    assert code == EXIT_ENV and "ffprobe" in err
    assert "svqa: hint: Run `svqa doctor`" in err
    # doctor itself reports the problem with its fix and exits 1, like any other "not ready"
    code, out, _err = run(capsys, "doctor", "--color", "never")
    assert code == EXIT_FAIL
    assert "SVQA_FFPROBE is set to" in out and "fix: point it at the ffprobe binary" in out


def test_unknown_profile_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _out, err = run(capsys, "check", str(tmp_path), "-p", "myspace")
    assert code == EXIT_USAGE and "unknown profile" in err


def test_fix_argument_rules(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    code, _out, err = run(capsys, "fix", str(a), "-p", "all")
    assert code == EXIT_USAGE and "one profile" in err
    code, _out, err = run(capsys, "fix", str(a), str(b), "-p", "tiktok", "-o", str(tmp_path / "x.mp4"))
    assert code == EXIT_USAGE and "--output" in err


@pytest.fixture
def fake_fix(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace FFmpeg and fix_file: records (input, output) and plans every file."""
    calls: list[tuple[str, str]] = []

    def fake(path, profile, runner, opts, *, log=None):
        output = output_for(path, profile, opts)
        calls.append((path, output))
        return FixResult(path, output, "planned")

    monkeypatch.setattr(cli, "_runner", lambda args: Runner(ffmpeg="ffmpeg", ffprobe="ffprobe"))
    monkeypatch.setattr(cli, "fix_file", fake)
    return calls


def test_fix_out_dir_keeps_sub_folders(capsys, tmp_path: Path, fake_fix: list[tuple[str, str]]) -> None:
    for sub in ("a", "b"):
        (tmp_path / "previews" / sub).mkdir(parents=True)
        (tmp_path / "previews" / sub / "clip.mp4").write_bytes(b"x")
    (tmp_path / "previews" / "top.mp4").write_bytes(b"x")
    up = tmp_path / "upload"
    code, out, _ = run(capsys, "fix", str(tmp_path / "previews"), "-p", "tiktok", "-O", str(up), "--format", "json")
    assert code == EXIT_OK, out
    assert [o for _p, o in fake_fix] == [
        str(up / "top.tiktok.mp4"),
        str(up / "a" / "clip.tiktok.mp4"),
        str(up / "b" / "clip.tiktok.mp4"),
    ]


def test_fix_refuses_two_inputs_with_one_output(capsys, tmp_path: Path, fake_fix: list[tuple[str, str]]) -> None:
    (tmp_path / "intro.mov").write_bytes(b"x")
    (tmp_path / "intro.mp4").write_bytes(b"x")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "clip.mp4").write_bytes(b"x")
    (b / "clip.mp4").write_bytes(b"x")
    code, out, _ = run(capsys, "fix", str(tmp_path / "intro.mov"), str(tmp_path / "intro.mp4"), "-p", "tiktok",
                       "--format", "json")  # fmt: skip
    doc = json.loads(out)
    assert code == EXIT_FAIL and [r["status"] for r in doc["results"]] == ["planned", "refused"]
    assert "also the output for" in doc["results"][1]["message"]
    # files named directly (not found in a folder) go straight into -O, so these collide too
    code, out, _ = run(capsys, "fix", str(a / "clip.mp4"), str(b / "clip.mp4"), "-p", "tiktok", "-O",
                       str(tmp_path / "up"), "--format", "json")  # fmt: skip
    assert code == EXIT_FAIL and [r["status"] for r in json.loads(out)["results"]] == ["planned", "refused"]
    # the same file named twice is processed once
    fake_fix.clear()
    code, out, _ = run(capsys, "fix", str(a / "clip.mp4"), str(a / ".." / "a" / "clip.mp4"), "-p", "tiktok",
                       "--format", "json")  # fmt: skip
    assert code == EXIT_OK and len(fake_fix) == 1 and len(json.loads(out)["results"]) == 1


def test_fix_errors_do_not_stop_the_batch(capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")

    def fake(path, profile, runner, opts, *, log=None):
        if path.endswith("a.mp4"):
            raise FixError("another `svqa fix` run is writing to the folder")
        return FixResult(path, output_for(path, profile, opts), "planned")

    monkeypatch.setattr(cli, "_runner", lambda args: Runner(ffmpeg="ffmpeg", ffprobe="ffprobe"))
    monkeypatch.setattr(cli, "fix_file", fake)
    code, out, _err = run(capsys, "fix", str(tmp_path), "-p", "tiktok", "--color", "never")
    assert code == EXIT_FAIL
    assert "ERROR" in out and "another `svqa fix` run" in out and "2 files: 1 ok, 1 not ok" in out


def test_collect_inputs_reports_sub_folders(tmp_path: Path) -> None:
    (tmp_path / "x" / "y").mkdir(parents=True)
    (tmp_path / "top.mp4").write_bytes(b"x")
    (tmp_path / "x" / "y" / "deep.mp4").write_bytes(b"x")
    found = [(Path(p).name, sub) for p, sub in collect_inputs([str(tmp_path), str(tmp_path / "top.mp4")])]
    assert found == [("top.mp4", ""), ("deep.mp4", str(Path("x") / "y")), ("top.mp4", "")]


def test_collect_files_skips_hidden_and_fix_outputs(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".hidden.mp4").write_bytes(b"x")
    (tmp_path / "a.tiktok.mp4").write_bytes(b"x")
    (tmp_path / "a.failed.mp4").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.MOV").write_bytes(b"x")
    (tmp_path / ".svqa-manifest.json").write_text(json.dumps({"entries": {"a.tiktok.mp4": {}}}))
    found = [Path(p).name for p in collect_files([str(tmp_path)])]
    assert found == ["a.mp4", "a.tiktok.mp4", "b.MOV"]
    found = [Path(p).name for p in collect_files([str(tmp_path)], skip_fix_outputs=True)]
    assert found == ["a.mp4", "b.MOV"]
    assert collect_files(["missing.mp4"]) == ["missing.mp4"]
