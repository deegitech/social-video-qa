"""Check registry and implementations.

A profile enables a check by listing its ID under ``rules`` with the check's
parameters. Each check returns an :class:`Outcome`:

* ``pass``  - the rule is met;
* ``hard``  - the rule is broken (reported at the rule's severity);
* ``soft``  - only a ``recommended`` value is missed (reported at ``warn`` or lower);
* ``skip``  - not applicable (no audio stream, not an MP4, not measured in fast mode);
* ``error`` - the measurement itself failed.

See docs/checks.md for what every check measures and how.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .hints import hint_for
from .probe import MediaInfo
from .util import MB, fmt_kbps, fmt_num, parse_ratio, parse_size, ratio_label

LEVELS = ("error", "warn", "info", "off")
RANK = {"off": 0, "info": 1, "warn": 2, "error": 3}
BASES = ("documented", "observed", "convention", "derived")


# ---------------------------------------------------------------------- parameter types
@dataclass(frozen=True)
class ParamType:
    label: str
    ok: Callable[[Any], bool]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_strs(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


NUM = ParamType("number", _is_num)
INT = ParamType("integer", _is_int)
STR = ParamType("string", lambda v: isinstance(v, str))
STRS = ParamType("list of strings", _is_strs)
INTS = ParamType("list of integers", lambda v: isinstance(v, list) and all(_is_int(x) for x in v))
SIZES = ParamType('list of "WxH" strings', lambda v: isinstance(v, list) and all(parse_size(x) for x in v))
RATIO = ParamType('ratio (number or "W:H" string)', lambda v: parse_ratio(v) is not None)
RATIOS = ParamType(
    "ratio or list of ratios",
    lambda v: (
        parse_ratio(v) is not None or (isinstance(v, list) and bool(v) and all(parse_ratio(x) is not None for x in v))
    ),
)
CODEC_STRS = ParamType(
    "mapping of codec name to list of strings",
    lambda v: isinstance(v, dict) and all(isinstance(k, str) and _is_strs(x) for k, x in v.items()),
)
CODEC_NUM = ParamType(
    "mapping of codec name to number",
    lambda v: isinstance(v, dict) and all(isinstance(k, str) and _is_num(x) for k, x in v.items()),
)

#: Keys every rule may carry in addition to its own parameters.
COMMON_PARAMS: dict[str, ParamType] = {
    "severity": ParamType("one of error, warn, info, off", lambda v: v in LEVELS),
    "note": STR,
    "basis": ParamType("one of " + ", ".join(BASES), lambda v: v in BASES),
    "source": STR,
}


# ---------------------------------------------------------------------- results
@dataclass
class Outcome:
    kind: str  # pass | hard | soft | skip | error
    message: str
    actual: Any = None
    expected: Any = None
    fixable: bool | None = None  # None: decided by FIX_STRATEGY


@dataclass
class Result:
    """One check applied to one file under one profile."""

    id: str
    status: str  # pass | fail | skip | error
    level: str | None  # error | warn | info  (set for fail and error)
    message: str
    actual: Any = None
    expected: Any = None
    note: str | None = None
    basis: str | None = None
    hint: str | None = None
    fixable: bool = False  # `svqa fix` can repair this failure

    @property
    def failed(self) -> bool:
        return self.status in ("fail", "error")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "status": self.status,
            "level": self.level,
            "message": self.message,
            "actual": self.actual,
            "expected": self.expected,
        }
        if self.failed:
            d["fixable"] = self.fixable
        for key in ("note", "basis", "hint"):
            value = getattr(self, key)
            if value:
                d[key] = value
        return d


@dataclass
class CheckReport:
    """All results for one file under one profile."""

    file: str
    profile: str
    profile_title: str = ""
    results: list[Result] = field(default_factory=list)
    error: str | None = None  # the file could not be analysed at all
    strict: bool = False
    profile_ref: str | None = None  # how the profile was named on the command line (ID or file path)

    def failures(self, levels: Iterable[str] = ("error", "warn")) -> list[Result]:
        wanted = set(levels)
        return [r for r in self.results if r.failed and r.level in wanted]

    @property
    def counts(self) -> dict[str, int]:
        c = {"error": 0, "warn": 0, "info": 0, "pass": 0, "skip": 0}
        for r in self.results:
            if r.failed and r.level in c:
                c[r.level] += 1
            elif r.status == "pass":
                c["pass"] += 1
            elif r.status == "skip":
                c["skip"] += 1
        return c

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        blocking = {"error", "warn"} if self.strict else {"error"}
        return not any(r.failed and r.level in blocking for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "profile": self.profile,
            "ok": self.ok,
            "error": self.error,
            "hint": hint_for(self.error),
            "counts": self.counts,
            "checks": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------- registry
@dataclass(frozen=True)
class CheckDef:
    id: str
    title: str
    severity: str
    params: dict[str, ParamType]
    needs: frozenset[str]
    hint: str
    doc: str
    fn: Callable[[dict[str, Any], MediaInfo], Outcome]
    describe: Callable[[dict[str, Any]], str]


REGISTRY: dict[str, CheckDef] = {}


def _register(
    cid: str,
    title: str,
    severity: str,
    params: dict[str, ParamType],
    *,
    doc: str,
    hint: str,
    needs: Iterable[str] = (),
    describe: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[[dict[str, Any], MediaInfo], Outcome]], Callable[[dict[str, Any], MediaInfo], Outcome]]:
    def deco(fn: Callable[[dict[str, Any], MediaInfo], Outcome]) -> Callable[[dict[str, Any], MediaInfo], Outcome]:
        REGISTRY[cid] = CheckDef(
            id=cid,
            title=title,
            severity=severity,
            params=params,
            needs=frozenset(needs),
            hint=hint,
            doc=doc,
            fn=fn,
            describe=describe or _describe_generic,
        )
        return fn

    return deco


# ---------------------------------------------------------------------- helpers
def _p(params: dict[str, Any], key: str | None) -> Any:
    return params.get(key) if key else None


def _range_text(lo: Any, hi: Any, unit: str, digits: int = 2) -> str:
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return f"{fmt_num(lo, digits)}-{fmt_num(hi, digits)}{u}"
    if lo is not None:
        return f">= {fmt_num(lo, digits)}{u}"
    if hi is not None:
        return f"<= {fmt_num(hi, digits)}{u}"
    return ""


def _range(
    value: float | None,
    p: dict[str, Any],
    *,
    what: str,
    unit: str,
    lo: str | None = "min",
    hi: str | None = "max",
    rlo: str | None = "recommended_min",
    rhi: str | None = "recommended_max",
    digits: int = 2,
    rel_eps: float = 1e-9,
) -> Outcome:
    if value is None:
        return Outcome("skip", f"{what} unknown")
    lo_v, hi_v, rlo_v, rhi_v = _p(p, lo), _p(p, hi), _p(p, rlo), _p(p, rhi)
    u = f" {unit}" if unit else ""
    shown = fmt_num(value, digits)

    def eps(x: float) -> float:
        return abs(x) * rel_eps + 1e-12

    def show(limit: float) -> str:
        # 128.3 vs a 128 limit must not read "128 is above 128"
        return fmt_num(value, digits + 2) if fmt_num(value, digits) == fmt_num(limit, digits) else shown

    allowed = _range_text(lo_v, hi_v, unit, digits)
    if lo_v is not None and value < lo_v - eps(lo_v):
        msg = f"{what} {show(lo_v)}{u} is below the minimum {fmt_num(lo_v, digits)}{u}"
        return Outcome("hard", msg, value, allowed)
    if hi_v is not None and value > hi_v + eps(hi_v):
        msg = f"{what} {show(hi_v)}{u} is above the maximum {fmt_num(hi_v, digits)}{u}"
        return Outcome("hard", msg, value, allowed)
    recommended = _range_text(rlo_v, rhi_v, unit, digits)
    if rlo_v is not None and value < rlo_v - eps(rlo_v):
        msg = f"{what} {show(rlo_v)}{u} is below the recommended {fmt_num(rlo_v, digits)}{u}"
        return Outcome("soft", msg, value, recommended)
    if rhi_v is not None and value > rhi_v + eps(rhi_v):
        msg = f"{what} {show(rhi_v)}{u} is above the recommended {fmt_num(rhi_v, digits)}{u}"
        return Outcome("soft", msg, value, recommended)
    shown_rule = allowed or recommended
    return Outcome("pass", f"{what} {shown}{u}" + (f" ({shown_rule})" if shown_rule else ""), value, shown_rule or None)


def _set(
    value: Any,
    p: dict[str, Any],
    *,
    what: str,
    show: Callable[[Any], str] = str,
) -> Outcome:
    if value is None:
        return Outcome("skip", f"{what} unknown")
    allowed, recommended = p.get("allowed"), p.get("recommended")

    def norm(x: Any) -> str:
        return str(x).strip().lower()

    if allowed is not None and norm(value) not in {norm(a) for a in allowed}:
        listed = ", ".join(show(a) for a in allowed)
        return Outcome("hard", f"{what} is {show(value)}; allowed: {listed}", value, allowed)
    if recommended is not None and norm(value) not in {norm(r) for r in recommended}:
        listed = ", ".join(show(r) for r in recommended)
        return Outcome("soft", f"{what} is {show(value)}; recommended: {listed}", value, recommended)
    return Outcome("pass", f"{what} {show(value)}", value, allowed if allowed is not None else recommended)


def _need(m: MediaInfo, *names: str) -> Outcome | None:
    for name in names:
        if name in m.measured:
            continue
        if name in m.analysis_errors:
            return Outcome("error", f"{name} analysis failed: {m.analysis_errors[name]}")
        return Outcome("skip", f"{name} not measured (fast mode)")
    return None


def _no_video(m: MediaInfo) -> Outcome | None:
    return None if m.video is not None else Outcome("skip", "no video stream")


def _no_audio(m: MediaInfo) -> Outcome | None:
    return None if m.audio is not None else Outcome("skip", "no audio stream")


def _iso(m: MediaInfo) -> Outcome | None:
    if m.boxes is None or not m.boxes.is_iso:
        return Outcome("skip", "not an MP4/MOV file")
    return None


_HANDLERS = {
    "vide": "video",
    "soun": "audio",
    "tmcd": "timecode",
    "meta": "metadata",
    "text": "text",
    "sbtl": "subtitle",
}


def _handler(code: str | None) -> str:
    return _HANDLERS.get(code or "", code or "unknown")


def _common_free(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k not in COMMON_PARAMS}


def _describe_generic(params: dict[str, Any]) -> str:
    rest = _common_free(params)
    if not rest:
        return "required"
    return ", ".join(f"{k}={v}" for k, v in rest.items())


def _describe_range(lo: str, hi: str, rlo: str | None, rhi: str | None, unit: str) -> Callable[[dict[str, Any]], str]:
    def describe(p: dict[str, Any]) -> str:
        parts = []
        hard = _range_text(p.get(lo), p.get(hi), unit)
        if hard:
            parts.append(hard)
        soft = _range_text(p.get(rlo) if rlo else None, p.get(rhi) if rhi else None, unit)
        if soft:
            parts.append(f"recommended {soft}")
        return "; ".join(parts) or "any"

    return describe


def _describe_set(show: Callable[[Any], str] = str) -> Callable[[dict[str, Any]], str]:
    def describe(p: dict[str, Any]) -> str:
        parts = []
        if p.get("allowed") is not None:
            parts.append(", ".join(show(a) for a in p["allowed"]))
        if p.get("recommended") is not None:
            parts.append("recommended " + ", ".join(show(r) for r in p["recommended"]))
        return "; ".join(parts) or "any"

    return describe


def _describe_fixed(text: str) -> Callable[[dict[str, Any]], str]:
    return lambda _p: text


# ====================================================================== checks
# ---------------------------------------------------------------- file & container
@_register(
    "file.size",
    "File size",
    "error",
    {"min_mb": NUM, "max_mb": NUM},
    doc="Size on disk in decimal megabytes (1 MB = 1,000,000 bytes, the stricter reading).",
    hint="Lower the bitrate or shorten the video. `svqa fix` caps the bitrate so the file fits.",
    describe=_describe_range("min_mb", "max_mb", None, None, "MB"),
)
def _file_size(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _range(
        m.size_bytes / MB, p, what="file size", unit="MB", lo="min_mb", hi="max_mb", rlo=None, rhi=None, digits=1
    )


@_register(
    "container.format",
    "Container format",
    "error",
    {"allowed": STRS, "recommended": STRS},
    doc="Container family: mp4, mov, m4v, 3gp (from the ftyp brand), webm, mkv, avi, ts.",
    hint="Remux into MP4: `svqa fix` does this without re-encoding when the codecs allow it.",
    describe=_describe_set(lambda x: str(x).upper()),
)
def _container_format(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _set(m.container, p, what="container", show=lambda x: str(x).upper())


@_register(
    "container.faststart",
    "moov before mdat (fast start)",
    "error",
    {},
    doc="Top-level box order: the moov (index) box must come before the first mdat (media data).",
    hint="Remux with `-movflags +faststart`; `svqa fix` does this without re-encoding.",
    describe=_describe_fixed("moov box before mdat"),
)
def _faststart(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _iso(m)
    if skip:
        return skip
    assert m.boxes is not None
    if m.boxes.moov_before_mdat is None:
        return Outcome("hard", "could not find both a moov and an mdat box (truncated or unusual file)")
    if not m.boxes.moov_before_mdat:
        return Outcome(
            "hard",
            "moov box is after mdat: the whole file must be read before playback or processing can start",
            "moov after mdat",
            "moov before mdat",
        )
    suffix = " (fragmented MP4)" if m.boxes.fragmented else ""
    return Outcome("pass", "moov before mdat" + suffix, "moov before mdat", "moov before mdat")


@_register(
    "container.edit_list",
    "No edit lists",
    "error",
    {},
    doc="No track may contain an edit list (elst box).",
    hint="Edit lists usually come from B-frames or AAC priming. `svqa fix` writes files without them.",
    describe=_describe_fixed("no elst boxes"),
)
def _edit_list(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _iso(m)
    if skip:
        return skip
    assert m.boxes is not None
    if m.boxes.moov_unreadable:
        return Outcome("error", "moov box too large to inspect")
    tracks = m.boxes.tracks_with_edit_lists
    if tracks:
        names = ", ".join(_handler(t.handler) for t in tracks)
        return Outcome("hard", f"edit list (elst) in {len(tracks)} track(s): {names}", len(tracks), 0)
    return Outcome("pass", "no edit lists", 0, 0)


@_register(
    "container.streams",
    "Stream count",
    "warn",
    {"max_video": INT, "max_audio": INT},
    doc="Number of video streams (cover-art pictures excluded) and audio streams.",
    hint="Keep one video and one audio stream; `svqa fix` drops the rest.",
    describe=lambda p: (
        ", ".join(f"<= {p[k]} {k.split('_')[1]}" for k in ("max_video", "max_audio") if k in p)
        or "at least one video stream"
    ),
)
def _streams(p: dict[str, Any], m: MediaInfo) -> Outcome:
    if m.video_streams == 0:
        return Outcome("hard", "no video stream", 0, ">= 1")
    problems = []
    if "max_video" in p and m.video_streams > p["max_video"]:
        problems.append(f"{m.video_streams} video streams (max {p['max_video']})")
    if "max_audio" in p and m.audio_streams > p["max_audio"]:
        problems.append(f"{m.audio_streams} audio streams (max {p['max_audio']})")
    actual = {"video": m.video_streams, "audio": m.audio_streams, "other": m.other_streams}
    if problems:
        return Outcome("hard", "; ".join(problems), actual)
    other = f", {m.other_streams} other" if m.other_streams else ""
    return Outcome("pass", f"{m.video_streams} video, {m.audio_streams} audio{other}", actual)


@_register(
    "container.tracks_enabled",
    "All tracks enabled",
    "warn",
    {},
    doc="Every track's tkhd 'enabled' flag is set (Apple: 'All tracks should be enabled').",
    hint="Remux the file; `svqa fix` writes enabled tracks only.",
    describe=_describe_fixed("all tracks enabled"),
)
def _tracks_enabled(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _iso(m)
    if skip:
        return skip
    assert m.boxes is not None
    disabled = m.boxes.disabled_tracks
    if disabled:
        names = ", ".join(_handler(t.handler) for t in disabled)
        return Outcome("hard", f"{len(disabled)} disabled track(s): {names}", len(disabled), 0)
    return Outcome("pass", "all tracks enabled", 0, 0)


@_register(
    "duration",
    "Duration",
    "error",
    {"min_s": NUM, "max_s": NUM, "recommended_min_s": NUM, "recommended_max_s": NUM},
    doc="Container duration in seconds.",
    hint="Edit the video. `svqa fix --trim` cuts videos that are too long (with a short audio fade).",
    describe=_describe_range("min_s", "max_s", "recommended_min_s", "recommended_max_s", "s"),
)
def _duration(p: dict[str, Any], m: MediaInfo) -> Outcome:
    out = _range(
        m.duration_s,
        p,
        what="duration",
        unit="s",
        lo="min_s",
        hi="max_s",
        rlo="recommended_min_s",
        rhi="recommended_max_s",
        digits=2,
    )
    # `svqa fix --trim` can shorten a video that is over the hard maximum; nothing can lengthen one.
    over_max = m.duration_s is not None and p.get("max_s") is not None and m.duration_s > p["max_s"]
    out.fixable = bool(over_max)
    return out


@_register(
    "bitrate.total",
    "Overall bitrate",
    "error",
    {"min_kbps": NUM, "max_kbps": NUM, "recommended_min_kbps": NUM, "recommended_max_kbps": NUM},
    doc="Average bitrate of the whole file (video + audio + overhead) in kbps.",
    hint="Re-encode at a different bitrate; `svqa fix` uses the profile's encode settings.",
    describe=_describe_range("min_kbps", "max_kbps", "recommended_min_kbps", "recommended_max_kbps", "kbps"),
)
def _bitrate_total(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _range(
        m.bitrate_kbps,
        p,
        what="overall bitrate",
        unit="kbps",
        lo="min_kbps",
        hi="max_kbps",
        rlo="recommended_min_kbps",
        rhi="recommended_max_kbps",
        digits=0,
    )


# ---------------------------------------------------------------- video
@_register(
    "video.codec",
    "Video codec",
    "error",
    {"allowed": STRS, "recommended": STRS},
    doc="FFprobe codec name of the first video stream (h264, hevc, vp9, av1, prores, ...).",
    hint="Re-encode to H.264 with `svqa fix`.",
    describe=_describe_set(),
)
def _video_codec(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_video(m) or _set(m.video.codec if m.video else None, p, what="video codec")


def _codec_map_check(p: dict[str, Any], codec: str | None, value: str | None, what: str) -> Outcome:
    table = {str(k).lower(): v for k, v in (p.get("allowed") or {}).items()}
    key = (codec or "").lower()
    if key not in table:
        return Outcome("skip", f"no {what} rule for {codec or 'unknown codec'}")
    if value is None:
        return Outcome("skip", f"{what} unknown")
    allowed = table[key]
    if value.strip().lower() not in {a.strip().lower() for a in allowed}:
        return Outcome("hard", f"{codec} {what} {value}; allowed: {', '.join(allowed)}", value, allowed)
    return Outcome("pass", f"{codec} {what} {value}", value, allowed)


def _describe_codec_map(p: dict[str, Any]) -> str:
    return "; ".join(f"{codec}: {', '.join(v)}" for codec, v in (p.get("allowed") or {}).items()) or "any"


@_register(
    "video.profile",
    "Video codec profile",
    "error",
    {"allowed": CODEC_STRS},
    doc="Codec profile per codec, as FFprobe names it (High, Main, Constrained Baseline, Main 10, ...).",
    hint="Re-encode with the right profile; `svqa fix` encodes H.264 High.",
    describe=_describe_codec_map,
)
def _video_profile(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    return _codec_map_check(p, m.video.codec, m.video.profile, "profile")


@_register(
    "video.level",
    "Video codec level",
    "error",
    {"max": CODEC_NUM},
    doc="Codec level per codec as a decimal (H.264 level 40 -> 4.0, HEVC 120 -> 4.0).",
    hint="Re-encode with a lower level (smaller frame size, frame rate or bitrate); `svqa fix` sets it.",
    describe=lambda p: "; ".join(f"{c} <= {fmt_num(v, 1)}" for c, v in (p.get("max") or {}).items()) or "any",
)
def _video_level(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    table = {str(k).lower(): float(v) for k, v in (p.get("max") or {}).items()}
    codec = (m.video.codec or "").lower()
    if codec not in table:
        return Outcome("skip", f"no level rule for {m.video.codec or 'unknown codec'}")
    if m.video.level is None:
        return Outcome("skip", "level unknown")
    limit = table[codec]
    if m.video.level > limit + 1e-9:
        return Outcome(
            "hard", f"{codec} level {m.video.level:.1f} is above {limit:.1f}", m.video.level, f"<= {limit:.1f}"
        )
    return Outcome("pass", f"{codec} level {m.video.level:.1f} (<= {limit:.1f})", m.video.level, f"<= {limit:.1f}")


@_register(
    "video.pix_fmt",
    "Pixel format",
    "error",
    {"allowed": STRS, "recommended": STRS},
    doc="Exact FFmpeg pixel format (yuv420p, yuvj420p, yuv420p10le, ...).",
    hint="Re-encode to yuv420p; `svqa fix` does.",
    describe=_describe_set(),
)
def _pix_fmt(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_video(m) or _set(m.video.pix_fmt if m.video else None, p, what="pixel format")


@_register(
    "video.chroma",
    "Chroma subsampling",
    "error",
    {"allowed": STRS, "recommended": STRS},
    doc="Chroma subsampling derived from the pixel format: 4:2:0, 4:2:2, 4:4:4, 4:0:0.",
    hint="Re-encode to 4:2:0 (yuv420p); `svqa fix` does.",
    describe=_describe_set(),
)
def _chroma(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_video(m) or _set(m.video.chroma if m.video else None, p, what="chroma subsampling")


@_register(
    "video.progressive",
    "Progressive scan",
    "error",
    {},
    doc="Field order must be progressive (or unsignalled); tt/bb/tb/bt mean interlaced.",
    hint="De-interlace and re-encode; `svqa fix` does (bwdif).",
    describe=_describe_fixed("progressive"),
)
def _progressive(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    fo = (m.video.field_order or "").lower()
    if fo in ("tt", "bb", "tb", "bt"):
        return Outcome("hard", f"interlaced video (field order {fo})", fo, "progressive")
    note = "" if fo == "progressive" else " (field order not signalled)"
    return Outcome("pass", "progressive" + note, fo or None, "progressive")


def _size_text(m: MediaInfo) -> str:
    assert m.video is not None
    v = m.video
    text = f"{v.display_width}x{v.display_height}"
    if v.rotation:
        text += f" (stored {v.width}x{v.height}, rotated {v.rotation} deg)"
    return text


@_register(
    "video.size",
    "Frame size",
    "error",
    {"allowed": SIZES, "recommended": SIZES},
    doc="Displayed frame size (after rotation metadata) against exact WxH lists.",
    hint="Scale/pad/crop to an accepted size; `svqa fix --fit pad|crop|blur` does.",
    describe=_describe_set(),
)
def _size(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    actual = (m.video.display_width, m.video.display_height)
    label = f"{actual[0]}x{actual[1]}"
    allowed = [parse_size(s) for s in p.get("allowed") or []]
    recommended = [parse_size(s) for s in p.get("recommended") or []]
    if p.get("allowed") is not None and actual not in allowed:
        return Outcome("hard", f"frame size {_size_text(m)}; allowed: {', '.join(p['allowed'])}", label, p["allowed"])
    if p.get("recommended") is not None and actual not in recommended:
        listed = ", ".join(p["recommended"])
        return Outcome("soft", f"frame size {_size_text(m)}; recommended: {listed}", label, p["recommended"])
    return Outcome("pass", f"frame size {_size_text(m)}", label, p.get("allowed") or p.get("recommended"))


@_register(
    "video.resolution",
    "Resolution limits",
    "error",
    {"min_width": INT, "max_width": INT, "min_height": INT, "max_height": INT},
    doc="Displayed width and height against minimum/maximum pixel limits.",
    hint="Scale the video; `svqa fix` does.",
    describe=lambda p: (
        ", ".join(
            f"{k.split('_')[1]} {'>=' if k.startswith('min') else '<='} {p[k]}"
            for k in ("min_width", "max_width", "min_height", "max_height")
            if k in p
        )
        or "any"
    ),
)
def _resolution(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    w, h = m.video.display_width, m.video.display_height
    problems = []
    for key, value, name in (
        ("min_width", w, "width"),
        ("max_width", w, "width"),
        ("min_height", h, "height"),
        ("max_height", h, "height"),
    ):
        if key not in p:
            continue
        limit = p[key]
        if key.startswith("min") and value < limit:
            problems.append(f"{name} {value} < {limit}")
        if key.startswith("max") and value > limit:
            problems.append(f"{name} {value} > {limit}")
    label = f"{w}x{h}"
    if problems:
        return Outcome("hard", f"frame size {_size_text(m)}: " + ", ".join(problems), label)
    return Outcome("pass", f"frame size {label} within limits", label)


def _describe_aspect(p: dict[str, Any]) -> str:
    parts = []
    lo, hi = parse_ratio(p.get("min")), parse_ratio(p.get("max"))
    if lo is not None and hi is not None:
        parts.append(f"{ratio_label(lo)} to {ratio_label(hi)} (w:h)")
    elif lo is not None:
        parts.append(f">= {ratio_label(lo)} (w:h)")
    elif hi is not None:
        parts.append(f"<= {ratio_label(hi)} (w:h)")
    rec = p.get("recommended")
    if rec is not None:
        recs = rec if isinstance(rec, list) else [rec]
        parts.append("recommended " + ", ".join(ratio_label(parse_ratio(r)) for r in recs))
    return "; ".join(parts) or "any"


@_register(
    "video.aspect",
    "Aspect ratio",
    "error",
    {"min": RATIO, "max": RATIO, "recommended": RATIOS, "tolerance": NUM},
    doc="Displayed width/height (rotation and pixel aspect applied). min/max are hard limits; "
    "'recommended' (default tolerance 1%) only warns.",
    hint="Pad, crop or blur-fill to the recommended ratio; `svqa fix --fit` does.",
    describe=_describe_aspect,
)
def _aspect(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    ar = m.video.display_aspect
    if not ar:
        return Outcome("skip", "aspect ratio unknown")
    label = ratio_label(ar)
    lo, hi = parse_ratio(p.get("min")), parse_ratio(p.get("max"))
    if lo is not None and ar < lo * (1 - 1e-6):
        return Outcome("hard", f"aspect ratio {label} is narrower than the minimum {ratio_label(lo)}", label)
    if hi is not None and ar > hi * (1 + 1e-6):
        return Outcome("hard", f"aspect ratio {label} is wider than the maximum {ratio_label(hi)}", label)
    rec = p.get("recommended")
    if rec is not None:
        targets = [parse_ratio(r) for r in (rec if isinstance(rec, list) else [rec])]
        tol = float(p.get("tolerance", 0.01))
        if not any(t and abs(ar - t) / t <= tol for t in targets):
            wanted = ", ".join(ratio_label(t) for t in targets if t)
            return Outcome(
                "soft", f"aspect ratio {label}; recommended {wanted} (may be cropped or letterboxed)", label, wanted
            )
    return Outcome("pass", f"aspect ratio {label}", label)


@_register(
    "video.sar",
    "Square pixels",
    "warn",
    {},
    doc="Sample (pixel) aspect ratio must be 1:1 or unset.",
    hint="Re-encode with square pixels; `svqa fix` resamples the picture to square pixels.",
    describe=_describe_fixed("SAR 1:1"),
)
def _sar(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    if m.video.sar in (None, "1:1"):
        return Outcome("pass", "square pixels", m.video.sar or "1:1", "1:1")
    return Outcome("hard", f"non-square pixels (SAR {m.video.sar})", m.video.sar, "1:1")


@_register(
    "video.rotation",
    "No rotation metadata",
    "info",
    {},
    doc="Rotation side data (display matrix) that asks players to rotate the stored picture.",
    hint="Bake the rotation into the pixels; `svqa fix` does (FFmpeg auto-rotates while re-encoding).",
    describe=_describe_fixed("no display-matrix rotation"),
)
def _rotation(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    if m.video.rotation:
        return Outcome(
            "hard",
            f"rotation metadata: stored {m.video.width}x{m.video.height}, players must rotate it "
            f"{m.video.rotation} deg",
            m.video.rotation,
            0,
        )
    return Outcome("pass", "no rotation metadata", 0, 0)


@_register(
    "video.fps",
    "Frame rate",
    "error",
    {"min": NUM, "max": NUM, "recommended_min": NUM, "recommended_max": NUM},
    needs=("packets",),
    doc="Frame rate as 1 / mean packet interval, first and last excluded (falls back to avg_frame_rate in fast mode).",
    hint="Convert the frame rate; `svqa fix` picks a cadence-friendly rate inside the allowed range.",
    describe=_describe_range("min", "max", "recommended_min", "recommended_max", "fps"),
)
def _fps(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_video(m) or _range(m.fps, p, what="frame rate", unit="fps", digits=3, rel_eps=0.001)


@_register(
    "video.cfr",
    "Constant frame rate",
    "warn",
    {"tolerance_pct": NUM, "max_irregular_pct": NUM},
    needs=("packets",),
    doc="Every frame interval (except the first and last) within tolerance_pct (default 2%) of the median.",
    hint="Convert to constant frame rate; `svqa fix` does (fps filter).",
    describe=_describe_fixed("constant frame rate"),
)
def _cfr(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m) or _need(m, "packets")
    if skip:
        return skip
    assert m.packets is not None
    tol = float(p.get("tolerance_pct", 2.0))
    bad, considered, worst = m.packets.irregular(tol)
    if considered == 0:
        return Outcome("skip", "too few frames to judge")
    pct = bad * 100.0 / considered
    med = m.packets.median_interval_ms or 0.0
    if pct > float(p.get("max_irregular_pct", 0.0)):
        return Outcome(
            "hard",
            f"variable frame rate: {bad} of {considered} frame intervals differ from {med:.2f} ms by more "
            f"than {fmt_num(tol)}% (worst {fmt_num(worst)} ms)",
            "variable",
            "constant",
        )
    return Outcome("pass", f"constant frame rate ({med:.2f} ms per frame)", "constant", "constant")


@_register(
    "video.bitrate",
    "Video bitrate",
    "error",
    {"min_kbps": NUM, "max_kbps": NUM, "recommended_min_kbps": NUM, "recommended_max_kbps": NUM},
    needs=("packets",),
    doc="Average video stream bitrate in kbps (container value, or packet sizes over duration).",
    hint="Re-encode with a bitrate cap; `svqa fix` does.",
    describe=_describe_range("min_kbps", "max_kbps", "recommended_min_kbps", "recommended_max_kbps", "kbps"),
)
def _video_bitrate(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    out = _range(
        m.video.bitrate_kbps,
        p,
        what="video bitrate",
        unit="kbps",
        lo="min_kbps",
        hi="max_kbps",
        rlo="recommended_min_kbps",
        rhi="recommended_max_kbps",
        digits=0,
    )
    if m.packets is not None and m.packets.peak_kbps_1s and out.kind != "skip":
        out.message += f"; 1-s peak {fmt_kbps(m.packets.peak_kbps_1s)}"
    return out


@_register(
    "video.b_frames",
    "No B-frames",
    "warn",
    {},
    needs=("packets",),
    doc="Picture reordering in packet timestamps (pts order differs from decode order), "
    "plus has_b_frames and the x264/x265 settings string as fallbacks.",
    hint="Re-encode without B-frames (`-bf 0`); `svqa fix` does.",
    describe=_describe_fixed("no B-frames / reordering"),
)
def _b_frames(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    assert m.video is not None
    enc = m.encoder_settings or {}
    if m.packets is not None:
        has, how = m.packets.reordered, "packet timestamps"
    else:
        has, how = bool(m.video.has_b_frames), "stream header"
        if not has and (enc.get("bframes") or 0) > 0:
            has, how = True, "encoder settings"
    if has:
        extra = f", {enc['encoder']} bframes={enc['bframes']}" if enc.get("bframes") else ""
        return Outcome("hard", f"B-frames present (from {how}{extra})", True, False)
    return Outcome("pass", "no B-frames", False, False)


@_register(
    "video.closed_gop",
    "Closed GOPs",
    "error",
    {},
    needs=("packets",),
    doc="No picture decoded after a keyframe may be displayed before it (open-GOP leading pictures); "
    "x264/x265 open_gop setting as extra evidence; the stream must start with a keyframe.",
    hint="Re-encode with closed GOPs; `svqa fix` does.",
    describe=_describe_fixed("closed GOPs, starts with a keyframe"),
)
def _closed_gop(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m)
    if skip:
        return skip
    enc = m.encoder_settings or {}
    open_sei = enc.get("open_gop") == 1
    if m.packets is None and not open_sei:
        need = _need(m, "packets")
        if need:
            return need
    leading = m.packets.leading_pictures if m.packets is not None else 0
    if leading or open_sei:
        parts = []
        if leading:
            parts.append(f"{leading} picture(s) decoded after a keyframe are shown before it")
        if open_sei:
            parts.append(f"{enc.get('encoder')} open_gop=1")
        return Outcome("hard", "open GOP: " + "; ".join(parts), "open", "closed")
    if m.packets is not None and m.packets.count and not m.packets.starts_with_keyframe:
        return Outcome("hard", "video does not start with a keyframe", "no keyframe at start", "closed")
    return Outcome("pass", "closed GOPs", "closed", "closed")


@_register(
    "video.keyint",
    "Keyframe interval",
    "warn",
    {"max_s": NUM},
    needs=("packets",),
    doc="Longest distance between keyframes, in seconds (the last GOP runs to the end of the video).",
    hint="Re-encode with a shorter GOP (`-g`); `svqa fix` uses the profile's keyint_s.",
    describe=lambda p: f"<= {fmt_num(p.get('max_s'))} s" if "max_s" in p else "any",
)
def _keyint(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m) or _need(m, "packets")
    if skip:
        return skip
    assert m.packets is not None
    return _range(
        m.packets.max_keyint_s,
        {"max": p.get("max_s")},
        what="longest keyframe interval",
        unit="s",
        digits=2,
        rel_eps=0.01,
    )


# ---------------------------------------------------------------- audio
@_register(
    "audio.present",
    "Audio track present",
    "error",
    {},
    doc="The file has at least one audio stream.",
    hint="Add an audio track (silence is fine); `svqa fix` adds a silent AAC track.",
    describe=_describe_fixed("audio stream required"),
)
def _audio_present(p: dict[str, Any], m: MediaInfo) -> Outcome:
    if m.audio is None:
        return Outcome("hard", "no audio stream", False, True)
    return Outcome("pass", f"audio stream present ({m.audio.codec})", True, True)


@_register(
    "audio.codec",
    "Audio codec",
    "error",
    {"allowed": STRS, "recommended": STRS},
    doc="FFprobe codec name of the first audio stream (aac, opus, mp3, pcm_s16le, ...).",
    hint="Re-encode the audio to AAC; `svqa fix` does this without touching the video.",
    describe=_describe_set(),
)
def _audio_codec(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_audio(m) or _set(m.audio.codec if m.audio else None, p, what="audio codec")


@_register(
    "audio.profile",
    "Audio codec profile",
    "warn",
    {"allowed": CODEC_STRS},
    doc="Audio profile per codec (AAC: LC, HE-AAC, HE-AACv2, ...).",
    hint="Re-encode as AAC-LC; `svqa fix` does.",
    describe=_describe_codec_map,
)
def _audio_profile(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m)
    if skip:
        return skip
    assert m.audio is not None
    return _codec_map_check(p, m.audio.codec, m.audio.profile, "profile")


@_register(
    "audio.sample_rate",
    "Audio sample rate",
    "error",
    {"allowed": INTS, "recommended": INTS, "min_hz": INT, "max_hz": INT},
    doc="Sample rate of the first audio stream in Hz.",
    hint="Resample (48000 Hz is the safe choice); `svqa fix` does.",
    describe=lambda p: (
        "; ".join(
            x
            for x in (
                ", ".join(str(v) for v in p["allowed"]) + " Hz" if "allowed" in p else "",
                _range_text(p.get("min_hz"), p.get("max_hz"), "Hz", 0),
                "recommended " + ", ".join(str(v) for v in p["recommended"]) + " Hz" if "recommended" in p else "",
            )
            if x
        )
        or "any"
    ),
)
def _sample_rate(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m)
    if skip:
        return skip
    assert m.audio is not None
    sr = m.audio.sample_rate
    if sr is None:
        return Outcome("skip", "sample rate unknown")
    if "allowed" in p and sr not in p["allowed"]:
        listed = ", ".join(str(v) for v in p["allowed"])
        return Outcome("hard", f"sample rate {sr} Hz; allowed: {listed} Hz", sr, p["allowed"])
    if "min_hz" in p and sr < p["min_hz"]:
        return Outcome("hard", f"sample rate {sr} Hz is below {p['min_hz']} Hz", sr)
    if "max_hz" in p and sr > p["max_hz"]:
        return Outcome("hard", f"sample rate {sr} Hz is above the maximum {p['max_hz']} Hz", sr)
    if "recommended" in p and sr not in p["recommended"]:
        listed = ", ".join(str(v) for v in p["recommended"])
        return Outcome("soft", f"sample rate {sr} Hz; recommended: {listed} Hz", sr, p["recommended"])
    return Outcome("pass", f"sample rate {sr} Hz", sr)


@_register(
    "audio.channels",
    "Audio channels",
    "error",
    {"allowed": INTS, "recommended": INTS},
    doc="Channel count of the first audio stream.",
    hint="Down- or up-mix (stereo is the safe choice); `svqa fix` does.",
    describe=_describe_set(lambda x: f"{x} ch"),
)
def _channels(p: dict[str, Any], m: MediaInfo) -> Outcome:
    return _no_audio(m) or _set(
        m.audio.channels if m.audio else None, p, what="audio channels", show=lambda x: f"{x} ch"
    )


@_register(
    "audio.bitrate",
    "Audio bitrate",
    "warn",
    {"min_kbps": NUM, "max_kbps": NUM, "recommended_min_kbps": NUM, "recommended_max_kbps": NUM},
    doc="Average bitrate of the first audio stream in kbps. Skipped when the loudness analysis finds the audio "
    "silent, because AAC encodes silence at about 2 kbps whatever bitrate was asked for.",
    hint="Re-encode the audio at the target bitrate; `svqa fix` does (and steps down if AAC overshoots).",
    describe=_describe_range("min_kbps", "max_kbps", "recommended_min_kbps", "recommended_max_kbps", "kbps"),
)
def _audio_bitrate(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m)
    if skip:
        return skip
    assert m.audio is not None
    if m.loudness is not None and m.loudness.silent:
        # AAC encodes digital silence at about 2 kbps whatever bitrate was asked for.
        return Outcome("skip", "audio is silent; bitrate not meaningful")
    return _range(
        m.audio.bitrate_kbps,
        p,
        what="audio bitrate",
        unit="kbps",
        lo="min_kbps",
        hi="max_kbps",
        rlo="recommended_min_kbps",
        rhi="recommended_max_kbps",
        digits=0,
    )


@_register(
    "audio.loudness",
    "Integrated loudness",
    "warn",
    {"target_lufs": NUM, "tolerance_lu": NUM},
    needs=("loudness",),
    doc="EBU R128 / ITU-R BS.1770 integrated loudness (FFmpeg ebur128) against target +/- tolerance.",
    hint="Normalise; `svqa fix` runs two-pass loudnorm and leaves the video untouched if it can.",
    describe=lambda p: f"{fmt_num(p.get('target_lufs'))} LUFS +/- {fmt_num(p.get('tolerance_lu', 1.0))} LU",
)
def _loudness(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m) or _need(m, "loudness")
    if skip:
        return skip
    assert m.loudness is not None
    if m.loudness.silent:
        return Outcome("skip", "audio is silent; loudness not meaningful")
    target = float(p["target_lufs"])
    tol = float(p.get("tolerance_lu", 1.0))
    i = m.loudness.integrated_lufs
    assert i is not None
    expected = f"{fmt_num(target)} +/- {fmt_num(tol)} LUFS"
    if abs(i - target) > tol + 1e-9:
        direction = "too loud" if i > target else "too quiet"
        return Outcome(
            "hard",
            f"integrated loudness {i:.1f} LUFS: {direction} by {abs(i - target):.1f} LU (target {expected})",
            i,
            expected,
        )
    return Outcome("pass", f"integrated loudness {i:.1f} LUFS (target {expected})", i, expected)


@_register(
    "audio.true_peak",
    "True peak",
    "warn",
    {"max_dbtp": NUM},
    needs=("loudness",),
    doc="Maximum true peak (4x oversampled, FFmpeg ebur128 peak=true) in dBTP.",
    hint="Leave headroom for the platform's lossy re-encode; `svqa fix` limits to about -1.5 dBTP.",
    describe=lambda p: f"<= {fmt_num(p.get('max_dbtp'))} dBTP",
)
def _true_peak(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m) or _need(m, "loudness")
    if skip:
        return skip
    assert m.loudness is not None
    tp = m.loudness.true_peak_dbtp
    limit = float(p["max_dbtp"])
    if tp is None or tp == float("-inf"):
        return Outcome("pass", "true peak -inf dBTP (silence)", None, f"<= {fmt_num(limit)} dBTP")
    if tp > limit + 1e-9:
        return Outcome(
            "hard",
            f"true peak {tp:.1f} dBTP is above {fmt_num(limit)} dBTP (risk of clipping after lossy re-encoding)",
            tp,
            f"<= {fmt_num(limit)} dBTP",
        )
    return Outcome("pass", f"true peak {tp:.1f} dBTP", tp, f"<= {fmt_num(limit)} dBTP")


@_register(
    "audio.hiss",
    "Hiss index (heuristic)",
    "warn",
    {"max_lufs": NUM, "max_relative_lu": NUM},
    needs=("hiss", "loudness"),
    doc="Integrated loudness of the audio after two 6 kHz high-pass filters. An observed heuristic "
    "(calibrated 2026-10), not a standard: on mixes normalised to about -14 LUFS, clean audio measured about "
    "-40 to -47 and audibly hissy audio about -26 to -30.",
    hint="Fix it in the mix (de-ess, low-pass noisy layers, mute noise-based effects). `svqa fix` cannot.",
    describe=lambda p: (
        "; ".join(
            x
            for x in (
                f"<= {fmt_num(p['max_lufs'])} LUFS" if "max_lufs" in p else "",
                f"<= {fmt_num(p['max_relative_lu'])} LU relative to programme" if "max_relative_lu" in p else "",
            )
            if x
        )
        or "measured only"
    ),
)
def _hiss(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m) or _need(m, "hiss", "loudness")
    if skip:
        return skip
    assert m.loudness is not None
    if m.loudness.silent:
        return Outcome("skip", "audio is silent")
    h = m.hiss_lufs
    if h is None:
        return Outcome("skip", "hiss index unknown")
    rel = h - m.loudness.integrated_lufs if m.loudness.integrated_lufs is not None else None
    detail = f"hiss index {h:.1f} LUFS" + (f" ({rel:+.1f} LU vs programme)" if rel is not None else "")
    if "max_lufs" in p and h > float(p["max_lufs"]) + 1e-9:
        return Outcome(
            "hard",
            f"{detail} is above {fmt_num(p['max_lufs'])} LUFS: "
            "strong energy above 6 kHz (hiss, noise, harsh sibilance)",
            h,
            f"<= {fmt_num(p['max_lufs'])} LUFS",
        )
    if "max_relative_lu" in p and rel is not None and rel > float(p["max_relative_lu"]) + 1e-9:
        return Outcome(
            "hard",
            f"{detail}: more than {fmt_num(p['max_relative_lu'])} LU relative to the programme",
            rel,
            f"<= {fmt_num(p['max_relative_lu'])} LU",
        )
    return Outcome("pass", detail, h)


@_register(
    "audio.silence",
    "Audio not silent",
    "info",
    {},
    needs=("loudness",),
    doc="Flags an audio track whose integrated loudness never rises above the -70 LUFS absolute gate.",
    hint="Check the export settings (muted track?). Intentional silence is fine; set severity to off.",
    describe=_describe_fixed("audio above -70 LUFS"),
)
def _silence(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_audio(m) or _need(m, "loudness")
    if skip:
        return skip
    assert m.loudness is not None
    if m.loudness.silent:
        return Outcome("hard", "audio track is silent (below -70 LUFS)", m.loudness.integrated_lufs, "> -70 LUFS")
    return Outcome("pass", "audio is not silent", m.loudness.integrated_lufs, "> -70 LUFS")


# ---------------------------------------------------------------- content
@_register(
    "content.black_start",
    "No black opening frames",
    "warn",
    {"max_s": NUM, "window_s": NUM},
    needs=("start",),
    doc="Length of black video at t=0 (FFmpeg blackdetect, 98% of pixels below 10% luma) within the first "
    "window_s seconds (default 3).",
    hint="Start on a real frame: feeds autoplay from the first frame and often use it as the cover.",
    describe=lambda p: f"<= {fmt_num(p.get('max_s', 0.1))} s of black at the start",
)
def _black_start(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m) or _need(m, "start")
    if skip:
        return skip
    assert m.start is not None
    limit = float(p.get("max_s", 0.1))
    black = m.start.black_start_s
    if black > limit + 1e-9:
        return Outcome(
            "hard",
            f"the video opens with {black:.2f} s of black (max {fmt_num(limit)} s); "
            "the first frame is often the default cover",
            black,
            f"<= {fmt_num(limit)} s",
        )
    return Outcome("pass", "opening frames are not black", black, f"<= {fmt_num(limit)} s")


@_register(
    "content.frozen_start",
    "No frozen opening",
    "warn",
    {"max_s": NUM, "window_s": NUM},
    needs=("start",),
    doc="Length of an unchanging picture at t=0 (FFmpeg freezedetect, -60 dB noise) within the first "
    "window_s seconds (default 3). A black opening is reported by content.black_start instead.",
    hint="Start with motion: the first second decides whether viewers keep watching.",
    describe=lambda p: f"<= {fmt_num(p.get('max_s', 1.0))} s of still picture at the start",
)
def _frozen_start(p: dict[str, Any], m: MediaInfo) -> Outcome:
    skip = _no_video(m) or _need(m, "start")
    if skip:
        return skip
    assert m.start is not None
    limit = float(p.get("max_s", 1.0))
    frozen = m.start.frozen_start_s
    frame = 1.0 / (m.fps or 30.0)
    if m.start.black_start_s and frozen <= m.start.black_start_s + 1.5 * frame:
        return Outcome("pass", "opening is black, not frozen (see content.black_start)", frozen)
    if frozen > limit + 1e-9:
        at_least = "at least " if frozen >= m.start.window_s - 1.5 * frame else ""
        return Outcome(
            "hard",
            f"the first {at_least}{frozen:.2f} s show a still picture (max {fmt_num(limit)} s)",
            frozen,
            f"<= {fmt_num(limit)} s",
        )
    return Outcome("pass", "opening has motion", frozen, f"<= {fmt_num(limit)} s")


# ---------------------------------------------------------------- metadata
@_register(
    "metadata.location",
    "No location metadata",
    "warn",
    {},
    doc="Container or stream tags that carry a recording location (e.g. com.apple.quicktime.location.ISO6709).",
    hint="Strip metadata before publishing; `svqa fix` strips all container metadata by default.",
    describe=_describe_fixed("no GPS/location tags"),
)
def _location(p: dict[str, Any], m: MediaInfo) -> Outcome:
    if m.location_tags:
        tags = ", ".join(m.location_tags)
        return Outcome(
            "hard",
            f"location metadata present ({tags}): it can reveal where the video was recorded",
            m.location_tags,
            [],
        )
    return Outcome("pass", "no location metadata", [], [])


# ====================================================================== fixability
#: How ``svqa fix`` repairs a failing check: ``remux`` (no re-encode), ``audio``
#: (re-encode the audio, copy the video) or ``video`` (re-encode the video).
#: ``duration`` is fixable with ``--trim`` when the video is too long; checks not
#: listed here (hiss, silence, black/frozen opening) cannot be fixed automatically.
FIX_STRATEGY: dict[str, str] = {
    "file.size": "video",
    "container.format": "remux",
    "container.faststart": "remux",
    "container.edit_list": "remux",
    "container.streams": "remux",
    "container.tracks_enabled": "remux",
    "bitrate.total": "video",
    "video.codec": "video",
    "video.profile": "video",
    "video.level": "video",
    "video.pix_fmt": "video",
    "video.chroma": "video",
    "video.progressive": "video",
    "video.size": "video",
    "video.resolution": "video",
    "video.aspect": "video",
    "video.sar": "video",
    "video.rotation": "video",
    "video.fps": "video",
    "video.cfr": "video",
    "video.bitrate": "video",
    "video.b_frames": "video",
    "video.closed_gop": "video",
    "video.keyint": "video",
    "audio.present": "audio",
    "audio.codec": "audio",
    "audio.profile": "audio",
    "audio.sample_rate": "audio",
    "audio.channels": "audio",
    "audio.bitrate": "audio",
    "audio.loudness": "audio",
    "audio.true_peak": "audio",
    "metadata.location": "remux",
}


def fixable(cid: str) -> bool:
    """True if ``svqa fix`` can repair this check (``duration`` only with ``--trim``)."""
    return cid in FIX_STRATEGY or cid == "duration"


# ====================================================================== engine
def window_for(rules: dict[str, dict[str, Any]]) -> float:
    """Seconds of video the start-content analysis must decode for these rules."""
    windows = [
        float(rules[c].get("window_s", 3.0)) for c in ("content.black_start", "content.frozen_start") if c in rules
    ]
    return max(windows) if windows else 3.0


def _matches(cid: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(cid, pat) for pat in patterns)


def effective_severity(cid: str, params: dict[str, Any], skip: Iterable[str] = (), only: Iterable[str] = ()) -> str:
    """Severity after ``--skip`` / ``--only`` filters."""
    only = list(only)
    if _matches(cid, skip) or (only and not _matches(cid, only)):
        return "off"
    return str(params.get("severity", REGISTRY[cid].severity))


def needs_for(rules: dict[str, dict[str, Any]], skip: Iterable[str] = (), only: Iterable[str] = ()) -> set[str]:
    """Analyses required by the enabled rules."""
    needs: set[str] = set()
    for cid, params in rules.items():
        if cid in REGISTRY and effective_severity(cid, params, skip, only) != "off":
            needs |= REGISTRY[cid].needs
    return needs


def run_checks(
    media: MediaInfo,
    rules: dict[str, dict[str, Any]],
    *,
    file: str,
    profile: str,
    profile_title: str = "",
    skip: Iterable[str] = (),
    only: Iterable[str] = (),
    strict: bool = False,
) -> CheckReport:
    """Apply ``rules`` (a profile's ``rules`` mapping) to ``media``."""
    skip, only = list(skip), list(only)
    report = CheckReport(file=file, profile=profile, profile_title=profile_title, strict=strict)
    if media.video is None:
        what = "audio-only file" if media.audio is not None else "no audio or video streams"
        report.error = f"no video stream ({what}); every profile here is for video"
        return report
    for cid, cdef in REGISTRY.items():
        if cid not in rules:
            continue
        params = rules[cid] or {}
        severity = effective_severity(cid, params, skip, only)
        note, basis = params.get("note"), params.get("basis")
        if severity == "off":
            report.results.append(Result(cid, "skip", None, "disabled", note=note, basis=basis))
            continue
        try:
            out = cdef.fn(params, media)
        except Exception as exc:  # a bug in a check must not hide the other results
            out = Outcome("error", f"internal error in check: {type(exc).__name__}: {exc}")
        if out.kind == "pass":
            status, level = "pass", None
        elif out.kind == "hard":
            status, level = "fail", severity
        elif out.kind == "soft":
            status, level = "fail", "warn" if RANK[severity] >= RANK["warn"] else severity
        elif out.kind == "error":
            status, level = "error", severity
        else:
            status, level = "skip", None
        failed = status in ("fail", "error")
        # A failed measurement gets the fix for its cause (a missing FFmpeg filter, a timeout...) when one is known.
        hint = (hint_for(out.message) if status == "error" else None) or (cdef.hint if failed else None)
        report.results.append(
            Result(
                id=cid,
                status=status,
                level=level,
                message=out.message,
                actual=out.actual,
                expected=out.expected,
                note=note,
                basis=basis,
                hint=hint,
                fixable=failed and (out.fixable if out.fixable is not None else cid in FIX_STRATEGY),
            )
        )
    return report


def describe_rule(cid: str, params: dict[str, Any]) -> str:
    """Short human description of a rule's requirement (used by `profiles show` and the docs)."""
    cdef = REGISTRY.get(cid)
    if cdef is None:
        return _describe_generic(params)
    try:
        return cdef.describe(params)
    except (KeyError, TypeError, ValueError):
        return _describe_generic(params)
