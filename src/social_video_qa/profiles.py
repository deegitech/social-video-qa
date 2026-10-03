"""Load, merge and validate platform profiles.

A profile is a JSON (built-in), YAML (needs PyYAML) or TOML (Python 3.11+, or tomli on 3.10) file::

    {
      "id": "my-reels",
      "extends": "instagram-reels-api",
      "title": "Our Reels",
      "rules": {"video.fps": {"min": 30, "max": 30}, "audio.hiss": null},
      "fix": {"video": {"crf": 18}}
    }

``extends`` takes one or more profile IDs or relative file paths. Parents are
merged left to right, then the child: mappings merge key by key, anything else
replaces, and ``null`` deletes the inherited key (so ``"audio.hiss": null``
switches a check off). Unknown keys are errors, so typos fail loudly.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from .checks import COMMON_PARAMS, REGISTRY, ParamType
from .util import parse_size, sha256_json

_toml: Any
if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # Python 3.10: TOML profiles work if the tomli backport is installed
    try:
        import tomli as _toml  # type: ignore[import-not-found,no-redef,unused-ignore]
    except ImportError:
        _toml = None

PROFILE_PATH_ENV = "SVQA_PROFILE_PATH"
EXTENSIONS = (".json", ".yaml", ".yml", ".toml")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOP_KEYS = {"$schema", "id", "title", "description", "platform", "extends", "sources", "notes", "rules", "fix"}
_SOURCE_KEYS = {"title", "url", "checked", "note"}


class ProfileError(ValueError):
    """A profile could not be found, parsed or validated."""


@dataclass
class Profile:
    id: str
    title: str
    description: str = ""
    platform: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    fix: dict[str, Any] = field(default_factory=dict)
    origin: str = "builtin"
    extends: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "platform": self.platform,
            "extends": self.extends,
            "sources": self.sources,
            "notes": self.notes,
            "rules": self.rules,
            "fix": self.fix,
        }

    @property
    def digest(self) -> str:
        """Hash of the resolved rules and fix settings (used to detect profile changes)."""
        return sha256_json({"rules": self.rules, "fix": self.fix})


@dataclass(frozen=True)
class ProfileRef:
    id: str
    title: str
    origin: str


# ---------------------------------------------------------------------- file loading
def _read_file(path: str) -> dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise ProfileError(f"cannot read profile {path}: {exc.strerror or exc}") from exc
    try:
        if ext == ".json":
            data = json.loads(raw.decode("utf-8"))
        elif ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-untyped]  # noqa: PLC0415 - optional dependency
            except ImportError as exc:
                raise ProfileError(f"{path}: YAML profiles need PyYAML (pip install 'social-video-qa[yaml]')") from exc
            data = yaml.safe_load(raw.decode("utf-8"))
        elif ext == ".toml":
            if _toml is None:
                raise ProfileError(
                    f"{path}: TOML profiles need Python 3.11+, or pip install 'social-video-qa[toml]' on Python 3.10"
                )
            data = _toml.loads(raw.decode("utf-8"))
        else:
            raise ProfileError(f"{path}: unsupported profile format (use {', '.join(EXTENSIONS)})")
    except ProfileError:
        raise
    except Exception as exc:  # json/yaml/toml syntax errors have different types
        raise ProfileError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: a profile must be a mapping at the top level")
    return data


def _builtin_root() -> Any:
    # one joinpath() argument at a time: Traversable.joinpath takes a single child on Python 3.10
    return resources.files("social_video_qa").joinpath("data").joinpath("profiles")


def _builtin_ids() -> list[str]:
    root = _builtin_root()
    return sorted(p.name[: -len(".json")] for p in root.iterdir() if p.name.endswith(".json"))


def _read_builtin(pid: str) -> dict[str, Any]:
    root = _builtin_root()
    entry = root.joinpath(pid + ".json")
    if not entry.is_file():
        raise ProfileError(f"unknown profile {pid!r}")
    data = json.loads(entry.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"built-in profile {pid} is malformed")
    return data


def search_dirs(extra: Iterable[str] = ()) -> list[str]:
    """Directories searched for profile files, in priority order."""
    dirs = [d for d in extra if d]
    env = os.environ.get(PROFILE_PATH_ENV, "")
    dirs += [d for d in env.split(os.pathsep) if d.strip()]
    return dirs


def _find_in_dirs(pid: str, dirs: Iterable[str]) -> str | None:
    for d in dirs:
        for ext in EXTENSIONS:
            candidate = os.path.join(d, pid + ext)
            if os.path.isfile(candidate):
                return candidate
    return None


def _looks_like_path(ref: str) -> bool:
    return os.sep in ref or "/" in ref or ref.lower().endswith(EXTENSIONS)


# ---------------------------------------------------------------------- merging
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base``: mappings recurse, ``None`` deletes, else replace."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_ref(ref: str, dirs: list[str], base_dir: str | None, builtin_only: bool) -> tuple[dict[str, Any], str, str]:
    """Locate and read one profile (no inheritance). Returns ``(data, id, origin)``."""
    if not builtin_only and _looks_like_path(ref):
        path = ref if os.path.isabs(ref) or base_dir is None else os.path.join(base_dir, ref)
        if not os.path.isfile(path):
            raise ProfileError(f"profile file not found: {ref}")
        data = _read_file(path)
        return data, str(data.get("id") or os.path.splitext(os.path.basename(path))[0]), os.path.normpath(path)
    found = None if builtin_only else _find_in_dirs(ref, dirs)
    if found:
        data = _read_file(found)
        return data, str(data.get("id") or ref), os.path.normpath(found)
    if ref not in _builtin_ids():
        raise ProfileError(f"unknown profile {ref!r}. Built-in profiles: {', '.join(_builtin_ids())}")
    return _read_builtin(ref), ref, "builtin"


def _resolve(
    ref: str,
    dirs: list[str],
    *,
    base_dir: str | None,
    stack: tuple[str, ...],
    builtin_only: bool = False,
) -> tuple[dict[str, Any], str, str]:
    """Return ``(merged data, id, origin)`` for a profile reference, following ``extends``."""
    data, pid, origin = _load_ref(ref, dirs, base_dir, builtin_only)
    key = origin if origin != "builtin" else "builtin:" + pid
    if key in stack:
        raise ProfileError("profile inheritance loop: " + " -> ".join([*stack, key]))
    parents = data.get("extends") or []
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, list) or not all(isinstance(x, str) for x in parents):
        raise ProfileError(f"{pid}: 'extends' must be a profile ID/path or a list of them")
    merged: dict[str, Any] = {}
    child_dir = os.path.dirname(origin) if origin != "builtin" else None
    for parent in parents:
        # Built-ins only inherit from built-ins, and a user file may extend the built-in it shadows.
        parent_builtin = origin == "builtin" or parent == pid
        pdata, _pid, _origin = _resolve(
            parent, dirs, base_dir=child_dir, stack=(*stack, key), builtin_only=parent_builtin
        )
        merged = deep_merge(merged, pdata)
    own = {k: v for k, v in data.items() if k != "extends"}
    if parents:
        # A child names itself: never inherit the parent's title or description.
        merged.pop("title", None)
        merged.pop("description", None)
        if "title" not in own:
            own["title"] = f"{pid} (based on {', '.join(parents)})"
    merged = deep_merge(merged, own)
    merged["id"] = pid
    merged["extends"] = list(parents)
    return merged, pid, origin


# ---------------------------------------------------------------------- validation
_FIX_SCHEMA: dict[str, Any] = {
    "size": ParamType('"WxH" string', lambda v: parse_size(v) is not None),
    "fit": ParamType("pad, crop or blur", lambda v: v in ("pad", "crop", "blur")),
    "pad_color": ParamType("colour name or hex", lambda v: isinstance(v, str) and bool(re.fullmatch(r"[#\w]+", v))),
    "fps": ParamType('number or "auto"', lambda v: v == "auto" or (isinstance(v, (int, float)) and v > 0)),
    "strip_metadata": ParamType("boolean", lambda v: isinstance(v, bool)),
    "video": {
        "encoder": ParamType('"libx264"', lambda v: v == "libx264"),
        "profile": ParamType("baseline, main or high", lambda v: v in ("baseline", "main", "high")),
        "level": ParamType(
            'level such as "4.0"',
            lambda v: isinstance(v, (str, int, float)) and bool(re.fullmatch(r"\d(\.\d)?", str(v))),
        ),
        "preset": ParamType(
            "x264 preset",
            lambda v: (
                v in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
            ),
        ),
        "crf": ParamType("number 0-51", lambda v: isinstance(v, (int, float)) and 0 <= v <= 51),
        "cbr_kbps": ParamType("positive number", lambda v: isinstance(v, (int, float)) and v > 0),
        "maxrate_kbps": ParamType("positive number", lambda v: isinstance(v, (int, float)) and v > 0),
        "bufsize_kbps": ParamType("positive number", lambda v: isinstance(v, (int, float)) and v > 0),
        "bframes": ParamType("integer 0-16", lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 16),
        "keyint_s": ParamType("positive number", lambda v: isinstance(v, (int, float)) and v > 0),
        "pix_fmt": ParamType("pixel format", lambda v: isinstance(v, str)),
    },
    "audio": {
        "encoder": ParamType('"aac"', lambda v: v == "aac"),
        "bitrate_kbps": ParamType("positive number", lambda v: isinstance(v, (int, float)) and v > 0),
        "sample_rate": ParamType("integer Hz", lambda v: isinstance(v, int) and v > 0),
        "channels": ParamType("1 or 2", lambda v: v in (1, 2)),
        "add_silent_track": ParamType("boolean", lambda v: isinstance(v, bool)),
    },
    "loudness": {
        "target_lufs": ParamType("number", lambda v: isinstance(v, (int, float)) and -70 < v < 0),
        "true_peak_dbtp": ParamType("number", lambda v: isinstance(v, (int, float)) and -9 <= v <= 0),
        "lra": ParamType("number 1-20", lambda v: isinstance(v, (int, float)) and 1 <= v <= 20),
    },
    "container": {
        "format": ParamType("mp4 or mov", lambda v: v in ("mp4", "mov")),
        "faststart": ParamType("boolean", lambda v: isinstance(v, bool)),
        "edit_list": ParamType("boolean", lambda v: isinstance(v, bool)),
    },
}


def _validate_mapping(data: Any, schema: dict[str, Any], where: str, errors: list[str]) -> None:
    if not isinstance(data, dict):
        errors.append(f"{where}: must be a mapping")
        return
    for key, value in data.items():
        spec = schema.get(key)
        if spec is None:
            errors.append(f"{where}.{key}: unknown key (known: {', '.join(sorted(schema))})")
        elif isinstance(spec, dict):
            if value is not None:
                _validate_mapping(value, spec, f"{where}.{key}", errors)
        elif value is not None and not spec.ok(value):
            errors.append(f"{where}.{key}: expected {spec.label}, got {value!r}")


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of problems with a resolved profile mapping (empty if valid)."""
    errors: list[str] = []
    for key in data:
        if key not in _TOP_KEYS and not str(key).startswith("x-"):
            errors.append(f"unknown top-level key {key!r} (known: {', '.join(sorted(_TOP_KEYS))})")
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        errors.append(f"id {pid!r} must be lowercase letters, digits, '.', '_' or '-' (max 64 chars)")
    for key in ("title", "description", "platform"):
        if key in data and not isinstance(data[key], str):
            errors.append(f"{key} must be a string")
    sources = data.get("sources", [])
    if not isinstance(sources, list):
        errors.append("sources must be a list")
    else:
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                errors.append(f"sources[{i}] must be a mapping")
                continue
            for k, v in src.items():
                if k not in _SOURCE_KEYS:
                    errors.append(f"sources[{i}].{k}: unknown key (known: {', '.join(sorted(_SOURCE_KEYS))})")
                elif not isinstance(v, str):
                    errors.append(f"sources[{i}].{k} must be a string")
    notes = data.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        errors.append("notes must be a list of strings")
    rules = data.get("rules", {})
    if not isinstance(rules, dict):
        errors.append("rules must be a mapping of check ID to parameters")
        rules = {}
    for cid, params in rules.items():
        cdef = REGISTRY.get(cid)
        if cdef is None:
            close = [c for c in REGISTRY if c.split(".")[-1] == str(cid).split(".")[-1]]
            hint = f" (did you mean {close[0]}?)" if close else ""
            errors.append(f"rules.{cid}: unknown check{hint}")
            continue
        if params is None:
            continue
        if not isinstance(params, dict):
            errors.append(f"rules.{cid}: parameters must be a mapping ({{}} enables the check with no parameters)")
            continue
        for key, value in params.items():
            spec = cdef.params.get(key) or COMMON_PARAMS.get(key)
            if spec is None:
                known = sorted([*cdef.params, *COMMON_PARAMS])
                errors.append(f"rules.{cid}.{key}: unknown parameter (known: {', '.join(known)})")
            elif not spec.ok(value):
                errors.append(f"rules.{cid}.{key}: expected {spec.label}, got {value!r}")
        if cid == "audio.loudness" and "target_lufs" not in params:
            errors.append("rules.audio.loudness: target_lufs is required")
        if cid == "audio.true_peak" and "max_dbtp" not in params:
            errors.append("rules.audio.true_peak: max_dbtp is required")
        if cid == "video.keyint" and "max_s" not in params:
            errors.append("rules.video.keyint: max_s is required")
    fix = data.get("fix", {})
    if fix is not None:
        _validate_mapping(fix, _FIX_SCHEMA, "fix", errors)
    return errors


def _to_profile(data: dict[str, Any], origin: str) -> Profile:
    rules = {cid: dict(params or {}) for cid, params in (data.get("rules") or {}).items() if params is not None}
    return Profile(
        id=str(data["id"]),
        title=str(data.get("title") or data["id"]),
        description=str(data.get("description") or ""),
        platform=str(data.get("platform") or ""),
        sources=list(data.get("sources") or []),
        notes=list(data.get("notes") or []),
        rules=rules,
        fix=dict(data.get("fix") or {}),
        origin=origin,
        extends=list(data.get("extends") or []),
    )


# ---------------------------------------------------------------------- public API
def _validated(data: dict[str, Any], origin: str) -> Profile:
    data.pop("$schema", None)
    errors = validate(data)
    if errors:
        bullet = "\n  - ".join(errors)
        raise ProfileError(f"profile {data.get('id')!r} ({origin}) is invalid:\n  - {bullet}")
    return _to_profile(data, origin)


def load_profile(ref: str, extra_dirs: Iterable[str] = ()) -> Profile:
    """Load a profile by ID (search dirs, then built-ins) or by file path, resolving ``extends``."""
    dirs = search_dirs(extra_dirs)
    data, _pid, origin = _resolve(ref, dirs, base_dir=None, stack=())
    return _validated(data, origin)


def load_builtin(pid: str) -> Profile:
    """Load a built-in profile, ignoring profile folders (a user file with the same ID does not shadow it)."""
    data, _pid, origin = _resolve(pid, [], base_dir=None, stack=(), builtin_only=True)
    return _validated(data, origin)


def list_profiles(extra_dirs: Iterable[str] = ()) -> list[ProfileRef]:
    """All profiles visible from the search dirs plus the built-ins (user files shadow built-ins)."""
    seen: dict[str, ProfileRef] = {}
    for d in search_dirs(extra_dirs):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in EXTENSIONS or stem in seen:
                continue
            path = os.path.join(d, name)
            try:
                data = _read_file(path)
                title = str(data.get("title") or stem)
            except ProfileError as exc:
                title = f"(unreadable: {exc})"
            seen[stem] = ProfileRef(stem, title, os.path.normpath(path))
    for pid in _builtin_ids():
        if pid not in seen:
            data = _read_builtin(pid)
            seen[pid] = ProfileRef(pid, str(data.get("title") or pid), "builtin")
    return sorted(seen.values(), key=lambda r: (r.origin != "builtin", r.id))


def builtin_ids() -> list[str]:
    return _builtin_ids()
