"""Profile loading, inheritance and validation (no FFmpeg)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from social_video_qa.checks import REGISTRY
from social_video_qa.profiles import (
    PROFILE_PATH_ENV,
    ProfileError,
    builtin_ids,
    deep_merge,
    list_profiles,
    load_profile,
    validate,
)

EXPECTED = {
    "instagram-reels-api",
    "instagram-story-api",
    "meta-ads-instagram",
    "app-store-preview-iphone",
    "app-store-preview-ipad",
    "youtube-shorts",
    "tiktok",
}


def write(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_builtin_profiles_present_and_valid() -> None:
    assert set(builtin_ids()) == EXPECTED
    for pid in builtin_ids():
        prof = load_profile(pid)
        assert prof.id == pid
        assert prof.title and prof.description and prof.sources
        for src in prof.sources:
            assert src["url"].startswith("https://")
            assert src["checked"]
        for cid, params in prof.rules.items():
            assert cid in REGISTRY
            assert params.get("basis") in ("documented", "observed", "convention", "derived"), (pid, cid)
        assert prof.fix.get("size"), pid


def test_key_platform_values() -> None:
    reels = load_profile("instagram-reels-api")
    assert reels.rules["duration"] == {"min_s": 3, "max_s": 900, "basis": "documented"}
    assert reels.rules["file.size"]["max_mb"] == 300
    story = load_profile("instagram-story-api")
    assert story.rules["duration"]["max_s"] == 60
    assert story.rules["file.size"]["max_mb"] == 100
    assert story.rules["video.closed_gop"] == reels.rules["video.closed_gop"]  # inherited
    assert story.title.startswith("Instagram Stories")  # own title, not the parent's
    iphone = load_profile("app-store-preview-iphone")
    assert iphone.rules["duration"]["min_s"] == 15 and iphone.rules["duration"]["max_s"] == 30
    assert iphone.rules["video.size"]["allowed"] == ["886x1920", "1920x886"]
    assert iphone.rules["video.level"]["max"]["h264"] == 4.0
    ipad = load_profile("app-store-preview-ipad")
    assert ipad.rules["video.size"]["allowed"] == ["1200x1600", "1600x1200"]
    assert ipad.fix["size"] == "1200x1600"
    assert ipad.fix["video"]["cbr_kbps"] == 10000  # inherited fix settings
    ads = load_profile("meta-ads-instagram")
    assert ads.rules["duration"]["max_s"] == 15
    assert ads.rules["duration"]["basis"] == "observed"
    assert load_profile("youtube-shorts").rules["duration"]["max_s"] == 180
    assert load_profile("tiktok").rules["duration"]["max_s"] == 600


def test_user_profile_extends_and_deletes(tmp_path: Path) -> None:
    path = write(
        tmp_path / "my-reels.json",
        {
            "extends": "instagram-reels-api",
            "rules": {"video.fps": {"min": 30, "max": 30}, "audio.hiss": None, "duration": {"max_s": 90}},
            "fix": {"video": {"crf": 18}},
        },
    )
    prof = load_profile(path)
    assert prof.id == "my-reels"
    assert prof.title == "my-reels (based on instagram-reels-api)"
    assert prof.description == ""
    assert "audio.hiss" not in prof.rules
    assert prof.rules["video.fps"]["min"] == 30
    assert prof.rules["duration"] == {"min_s": 3, "max_s": 90, "basis": "documented"}
    assert prof.fix["video"]["crf"] == 18
    assert prof.fix["video"]["maxrate_kbps"] == 8000
    assert prof.origin.endswith("my-reels.json")


def test_profile_dir_search_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "house.json", {"title": "House style", "rules": {"duration": {"max_s": 30}}})
    assert load_profile("house", [str(tmp_path)]).title == "House style"
    with pytest.raises(ProfileError):
        load_profile("house")
    monkeypatch.setenv(PROFILE_PATH_ENV, str(tmp_path))
    assert load_profile("house").rules["duration"]["max_s"] == 30
    refs = {r.id: r for r in list_profiles()}
    assert refs["house"].origin.endswith("house.json")
    assert refs["tiktok"].origin == "builtin"


def test_user_file_can_shadow_and_extend_builtin(tmp_path: Path) -> None:
    write(tmp_path / "tiktok.json", {"extends": "tiktok", "title": "Our TikTok", "rules": {"duration": {"max_s": 60}}})
    prof = load_profile("tiktok", [str(tmp_path)])
    assert prof.title == "Our TikTok"
    assert prof.rules["duration"]["max_s"] == 60
    assert prof.rules["duration"]["recommended_max_s"] == 180  # from the built-in parent


def test_builtin_parents_are_not_hijacked(tmp_path: Path) -> None:
    write(tmp_path / "instagram-reels-api.json", {"title": "evil", "rules": {"duration": {"max_s": 1}}})
    story = load_profile("instagram-story-api", [str(tmp_path)])
    assert story.rules["container.faststart"] == {"basis": "documented"}
    assert story.rules["duration"]["max_s"] == 60


def test_relative_extends_path_and_loop(tmp_path: Path) -> None:
    write(tmp_path / "base.json", {"rules": {"duration": {"max_s": 20}}})
    write(tmp_path / "child.json", {"extends": "./base.json", "rules": {"duration": {"min_s": 5}}})
    assert load_profile(str(tmp_path / "child.json")).rules["duration"] == {"max_s": 20, "min_s": 5}
    write(tmp_path / "a.json", {"extends": "./b.json"})
    write(tmp_path / "b.json", {"extends": "./a.json"})
    with pytest.raises(ProfileError, match="loop"):
        load_profile(str(tmp_path / "a.json"))


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"rules": {"video.fsp": {}}}, "unknown check"),
        ({"rules": {"fps": {}}}, "did you mean video.fps"),
        ({"rules": {"duration": {"max": 30}}}, "unknown parameter"),
        ({"rules": {"duration": {"max_s": "30"}}}, "expected number"),
        ({"rules": {"duration": {"severity": "fatal"}}}, "severity"),
        ({"rules": {"audio.loudness": {"tolerance_lu": 1}}}, "target_lufs is required"),
        ({"rules": {"video.size": {"allowed": ["1080:1920"]}}}, "WxH"),
        ({"rules": "nope"}, "rules must be a mapping"),
        ({"colour": "blue"}, "unknown top-level key"),
        ({"fix": {"video": {"crf": 99}}}, "fix.video.crf"),
        ({"fix": {"videos": {}}}, "fix.videos: unknown key"),
        ({"sources": [{"title": "x", "link": "y"}]}, "sources[0].link"),
        ({"notes": "one"}, "notes must be a list"),
    ],
)
def test_validation_errors(data: dict, fragment: str) -> None:
    errors = validate({"id": "x", **data})
    assert any(fragment in e for e in errors), errors


def test_x_keys_and_schema_key_allowed() -> None:
    assert validate({"id": "x", "x-team": "video", "$schema": "./schema.json"}) == []


def test_invalid_id() -> None:
    assert any("id" in e for e in validate({"id": "Bad ID"}))


def test_invalid_profile_file_reports_all_problems(tmp_path: Path) -> None:
    path = write(tmp_path / "bad.json", {"rules": {"duration": {"max": 1}, "nope": {}}})
    with pytest.raises(ProfileError) as exc:
        load_profile(path)
    assert "rules.duration.max" in str(exc.value) and "rules.nope" in str(exc.value)


def test_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProfileError, match="cannot parse"):
        load_profile(str(tmp_path / "broken.json"))
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ProfileError, match="mapping"):
        load_profile(str(tmp_path / "list.json"))
    with pytest.raises(ProfileError, match="not found"):
        load_profile(str(tmp_path / "missing.json"))
    with pytest.raises(ProfileError, match="unknown profile"):
        load_profile("no-such-platform")


def test_yaml_profile(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    path = tmp_path / "shorts.yaml"
    path.write_text(
        "extends: youtube-shorts\nrules:\n  duration:\n    max_s: 60\n  audio.hiss: null\n", encoding="utf-8"
    )
    prof = load_profile(str(path))
    assert prof.rules["duration"]["max_s"] == 60
    assert "audio.hiss" not in prof.rules


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs Python 3.11")
def test_toml_profile(tmp_path: Path) -> None:
    path = tmp_path / "ads.toml"
    path.write_text('extends = "meta-ads-instagram"\n[rules.duration]\nmax_s = 30\n', encoding="utf-8")
    assert load_profile(str(path)).rules["duration"]["max_s"] == 30


def test_deep_merge_semantics() -> None:
    base = {"a": {"b": 1, "c": 2}, "l": [1, 2], "x": 1}
    out = deep_merge(base, {"a": {"c": None, "d": 4}, "l": [3], "x": None})
    assert out == {"a": {"b": 1, "d": 4}, "l": [3]}
    assert base == {"a": {"b": 1, "c": 2}, "l": [1, 2], "x": 1}  # input untouched


def test_profile_digest_changes_with_rules() -> None:
    a = load_profile("tiktok")
    b = load_profile("tiktok")
    assert a.digest == b.digest
    b.rules["duration"]["max_s"] = 10
    assert a.digest != b.digest


def test_examples_directory_profiles_are_valid() -> None:
    examples = Path(__file__).resolve().parent.parent / "examples" / "profiles"
    files = sorted(p for p in examples.iterdir() if p.suffix in (".json", ".yaml", ".yml", ".toml"))
    assert files, "examples/profiles should contain sample profiles"
    for path in files:
        if path.suffix == ".toml" and sys.version_info < (3, 11):
            continue
        if path.suffix in (".yaml", ".yml"):
            pytest.importorskip("yaml")
        load_profile(str(path))
