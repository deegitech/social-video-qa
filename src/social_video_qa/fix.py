"""Plan and apply fixes: remux or re-encode a video so it passes a profile, then verify it.

Pipeline for one file::

    analyse source -> check -> plan (cheapest mode that can fix the failures)
        -> [dry run stops here and prints the plan]
        -> lock output dir -> journal "encoding" -> loudnorm pass 1 -> encode to a temp file
        -> step audio bitrate down if AAC overshoots -> re-check the temp file
        -> publish with an atomic rename only if no error-level check fails
        -> journal "ok" with hashes (re-runs with the same input and recipe are skipped)

Modes, from cheapest to most expensive:

* ``remux``: copy both streams; fixes moov position, edit lists (when the video has
  no B-frames), container, extra streams and metadata.
* ``audio``: copy the video, re-encode the audio (codec, rate, channels, bitrate,
  two-pass loudness normalisation, adding a silent track).
* ``full``: re-encode the video with libx264 (and the audio when it needs it).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shlex
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from . import __version__
from .checks import FIX_STRATEGY, RANK, CheckReport, Result, effective_severity, needs_for, run_checks, window_for
from .ffmpeg import FFmpegError, Runner, hardened_input, tail
from .hints import hint_for
from .parsers import parse_fraction, parse_loudnorm_json
from .probe import MediaInfo, ProbeError, VideoInfo, analyze
from .profiles import Profile, deep_merge
from .util import atomic_write_text, fmt_num, parse_size, sha256_file, sha256_json, utc_now

MANIFEST_NAME = ".svqa-manifest.json"
LOCK_NAME = ".svqa-manifest.lock"
MANIFEST_SCHEMA = "social-video-qa/fix-manifest@1"
MODES = ("auto", "remux", "audio", "full")

#: Codecs that can be stream-copied into MP4.
MP4_VIDEO_COPY = {"h264", "hevc", "av1", "vp9", "mpeg4"}
MP4_AUDIO_COPY = {"aac", "mp3", "opus", "ac3", "eac3", "alac"}
MOV_VIDEO_COPY = MP4_VIDEO_COPY | {"prores", "mjpeg"}
MOV_AUDIO_COPY = MP4_AUDIO_COPY | {"pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_s32be"}

STANDARD_RATES = [
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
]

DEFAULT_FIX: dict[str, Any] = {
    "fit": "pad",
    "pad_color": "black",
    "fps": "auto",
    "strip_metadata": True,
    "video": {
        "encoder": "libx264",
        "profile": "high",
        "preset": "medium",
        "crf": 20,
        "bframes": 0,
        "keyint_s": 2,
        "pix_fmt": "yuv420p",
    },
    "audio": {"encoder": "aac", "bitrate_kbps": 128, "sample_rate": 48000, "channels": 2, "add_silent_track": False},
    "loudness": {"target_lufs": -14, "true_peak_dbtp": -1.5, "lra": 11},
    "container": {"format": "mp4", "faststart": True, "edit_list": False},
}


class FixError(RuntimeError):
    """A fix could not be planned or applied."""


@dataclass
class FixOptions:
    apply: bool = False
    output: str | None = None
    out_dir: str | None = None
    mode: str = "auto"
    fit: str | None = None
    trim: bool = False
    preset: str | None = None
    crf: float | None = None
    loudnorm: bool = True
    keep_metadata: bool = False
    force: bool = False
    keep_failed: bool = False
    manifest: bool = True
    strict: bool = False
    skip: list[str] = field(default_factory=list)
    only: list[str] = field(default_factory=list)


@dataclass
class FixPlan:
    source: str
    output: str
    profile: str
    mode: str  # none | remux | audio | full
    video_action: str  # copy | encode
    audio_action: str  # copy | encode | silent | none
    reasons: dict[str, list[str]]
    unfixed: list[Result]
    blockers: list[str]
    notes: list[str]
    video: dict[str, Any]
    audio: dict[str, Any]
    loudness: dict[str, Any] | None
    container: dict[str, Any]
    strip_metadata: bool
    trim_s: float | None
    recipe: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "output": self.output,
            "profile": self.profile,
            "mode": self.mode,
            "video_action": self.video_action,
            "audio_action": self.audio_action,
            "reasons": self.reasons,
            "unfixed": [r.id for r in self.unfixed],
            "blockers": self.blockers,
            "notes": self.notes,
            "video": self.video,
            "audio": self.audio,
            "loudness": self.loudness,
            "container": self.container,
            "strip_metadata": self.strip_metadata,
            "trim_s": self.trim_s,
            "recipe": self.recipe,
        }


@dataclass
class FixResult:
    source: str
    output: str | None
    status: str  # planned | nothing-to-do | up-to-date | written | failed | refused | error
    message: str = ""
    plan: FixPlan | None = None
    before: CheckReport | None = None
    after: CheckReport | None = None
    commands: list[list[str]] = field(default_factory=list)
    elapsed_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("planned", "nothing-to-do", "up-to-date", "written")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "output": self.output,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "hint": None if self.ok else hint_for(self.message),
            "plan": self.plan.to_dict() if self.plan else None,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "commands": [shlex.join(c) for c in self.commands],
            "elapsed_s": round(self.elapsed_s, 2) if self.elapsed_s is not None else None,
        }


# ---------------------------------------------------------------------- helpers
def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def _within(value: float, lo: float | None, hi: float | None, slack: float = 0.001) -> bool:
    if lo is not None and value < lo * (1 - slack):
        return False
    return not (hi is not None and value > hi * (1 + slack))


def choose_fps(
    src: float | None,
    lo: float | None = None,
    hi: float | None = None,
    rlo: float | None = None,
    rhi: float | None = None,
) -> Fraction:
    """Pick an output frame rate: keep the source rate when allowed, else a cadence-friendly standard rate."""
    lo_eff = max((x for x in (lo, rlo) if x is not None), default=None)
    hi_eff = min((x for x in (hi, rhi) if x is not None), default=None)
    candidates = [r for r in STANDARD_RATES if _within(float(r), lo_eff, hi_eff)]
    if src and src > 0:
        near = [r for r in STANDARD_RATES if abs(float(r) - src) / src < 0.005]
        snapped = min(near, key=lambda r: abs(float(r) - src)) if near else Fraction(src).limit_denominator(1001)
        if _within(float(snapped), lo_eff, hi_eff):
            return snapped
        # prefer an exact cadence (59.94 -> 29.97, 50 -> 25), then the higher rate
        cadence = []
        for r in candidates:
            ratio = src / float(r)
            err = abs(ratio - round(ratio))
            if ratio >= 1 - 1e-9 and err < 0.01:
                cadence.append((round(err, 6), -float(r), r))
        if cadence:
            return min(cadence)[2]
        below = [r for r in candidates if float(r) <= src]
        if below:
            return max(below)
        if candidates:
            return min(candidates)
    if _within(30.0, lo_eff, hi_eff):
        return Fraction(30)
    return candidates[0] if candidates else Fraction(30)


def fraction_text(f: Fraction) -> str:
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


def default_output(source: str, profile_id: str, opts: FixOptions, container: str = "mp4") -> str:
    if opts.output:
        return opts.output
    stem = os.path.splitext(os.path.basename(source))[0]
    directory = opts.out_dir or os.path.dirname(source)
    name = f"{stem}.{profile_id}.{container}"
    return os.path.join(directory, name) if directory else name


def output_format(profile: Profile) -> str:
    """Container the profile's fix settings write (``mp4`` unless the profile says ``mov``)."""
    return str(deep_merge(DEFAULT_FIX, profile.fix)["container"].get("format", "mp4"))


def output_for(source: str, profile: Profile, opts: FixOptions) -> str:
    """Where ``svqa fix`` writes the fixed copy of ``source``."""
    return default_output(source, profile.id, opts, output_format(profile))


def source_path_key(source: str) -> str:
    """SHA-256 of the source's resolved path: identifies a source in the manifest without storing the path."""
    return hashlib.sha256(os.fsencode(os.path.realpath(source))).hexdigest()


def square_pixel_size(v: VideoInfo) -> tuple[int, int]:
    """Displayed size in square pixels: rotation applied, and the sample aspect ratio resolved by resampling.

    An anamorphic 1440x1080 source with SAR 4:3 is shown as 1920x1080, so this returns ``(1920, 1080)``.
    """
    dw, dh = v.display_width, v.display_height
    sar = parse_fraction(v.sar.replace(":", "/")) if v.sar else None
    if not sar or abs(sar - 1.0) < 1e-6 or not dw or not dh:
        return dw, dh
    if v.rotation in (90, 270):
        return dw, _even(round(dh * sar))  # the SAR stretches the coded width, which is now vertical
    return _even(round(dw * sar)), dh


#: Transfer functions of HDR video (PQ and HLG, the iPhone default).
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _same_file(a: str, b: str) -> bool:
    if os.path.abspath(a) == os.path.abspath(b):
        return True
    try:
        return os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


# ---------------------------------------------------------------------- planning
def plan_fix(media: MediaInfo, report: CheckReport, profile: Profile, opts: FixOptions, output: str) -> FixPlan:
    """Decide what to change. Pure function of the measurements, the profile and the options."""
    cfg = deep_merge(DEFAULT_FIX, profile.fix)
    rules = {cid: p for cid, p in profile.rules.items() if effective_severity(cid, p, opts.skip, opts.only) != "off"}
    failing = [r for r in report.results if r.failed and r.level in ("error", "warn")]
    reasons: dict[str, list[str]] = {"remux": [], "audio": [], "video": []}
    unfixed: list[Result] = []
    blockers: list[str] = []
    notes: list[str] = []
    trim_s: float | None = None
    # Failures the output would still have block the fix when they would fail verification:
    # errors always, warnings too with --strict.
    blocking = ("error", "warn") if opts.strict else ("error",)

    for r in failing:
        if r.id == "duration":
            rule = rules.get("duration", {})
            dur = media.duration_s or 0.0
            too_long = "max_s" in rule and dur > float(rule["max_s"])
            if too_long and opts.trim:
                # 0.1 s of margin: AAC frames and muxing can add a few hundredths of a second
                trim_s = max(float(rule.get("min_s", 0.0)), float(rule["max_s"]) - 0.1)
                reasons["video"].append(r.id)
            else:
                unfixed.append(r)
                if r.level in blocking:
                    if too_long:
                        tip = " (use --trim to cut it)"
                    elif r.level == "warn":
                        tip = " (--strict treats warnings as failures)"
                    else:
                        tip = ""
                    blockers.append(f"{r.message}{tip}")
            continue
        strategy = FIX_STRATEGY.get(r.id)
        if strategy == "remux" and r.id == "container.edit_list" and media.video_reordered:
            strategy = "video"  # dropping the edit list of a B-frame stream would shift A/V sync
        if strategy is None:
            unfixed.append(r)
            if r.level in blocking:
                strict = " and --strict treats warnings as failures" if r.level == "warn" else ""
                blockers.append(f"{r.id}: {r.message} (cannot be fixed automatically{strict})")
            continue
        reasons[strategy].append(r.id)

    fmt = str(cfg["container"].get("format", "mp4"))
    video_copyable = MP4_VIDEO_COPY if fmt == "mp4" else MOV_VIDEO_COPY
    audio_copyable = MP4_AUDIO_COPY if fmt == "mp4" else MOV_AUDIO_COPY
    no_editlist = not cfg["container"].get("edit_list", False)

    # ------------------------------------------------ actions
    if media.video is None:
        raise FixError("the file has no video stream")
    if opts.mode in ("remux", "audio"):
        video_action = "copy"
    elif opts.mode == "full" or reasons["video"]:
        video_action = "encode"
    else:
        video_action = "copy"
    wants_audio = "audio.present" in rules or bool(cfg["audio"].get("add_silent_track"))
    if media.audio is None:
        audio_action = "silent" if wants_audio and opts.mode != "remux" else "none"
    elif opts.mode == "remux":
        audio_action = "copy"
    elif opts.mode in ("audio", "full") or reasons["audio"] or trim_s is not None:
        audio_action = "encode"
    else:
        audio_action = "copy"
    writing = (
        video_action == "encode"
        or audio_action == "encode"
        or (audio_action == "silent" and "audio.present" in reasons["audio"])
        or bool(reasons["remux"])
        or opts.mode != "auto"
    )
    if not writing:
        audio_action = "none" if media.audio is None else "copy"

    # Escalations only matter when a file is going to be written.
    if writing and video_action == "copy" and (media.video.codec or "") not in video_copyable:
        if opts.mode in ("remux", "audio"):
            blockers.append(f"{media.video.codec} video cannot be copied into {fmt.upper()}; use --mode full")
        else:
            video_action = "encode"
            notes.append(f"{media.video.codec} video cannot be copied into {fmt.upper()}, so it is re-encoded")
    if writing and video_action == "copy" and media.video_reordered and no_editlist and "container.edit_list" in rules:
        if opts.mode in ("remux", "audio"):
            blockers.append(
                "the video has B-frames and the profile forbids edit lists, so it must be re-encoded (use --mode auto)"
            )
        else:
            video_action = "encode"
            notes.append("the video has B-frames; it is re-encoded so the output needs no edit list")
    if (
        writing
        and audio_action == "copy"
        and media.audio is not None
        and (media.audio.codec or "") not in audio_copyable
    ):
        if opts.mode == "remux":
            blockers.append(f"{media.audio.codec} audio cannot be copied into {fmt.upper()}; use --mode audio")
        else:
            audio_action = "encode"
            notes.append(f"{media.audio.codec} audio cannot be copied into {fmt.upper()}, so it is re-encoded")

    # forced modes cannot fix everything
    if opts.mode == "remux":
        for cid in reasons["audio"] + reasons["video"]:
            blockers.append(f"{cid} needs re-encoding; --mode remux copies streams (use --mode auto)")
    elif opts.mode == "audio":
        for cid in reasons["video"]:
            blockers.append(f"{cid} needs a video re-encode; --mode audio copies the video (use --mode auto)")

    # ------------------------------------------------ mode
    if video_action == "encode":
        mode = "full"
    elif audio_action in ("encode", "silent"):
        mode = "audio"
    elif writing:
        mode = "remux"
    else:
        mode = "none"

    # ------------------------------------------------ video settings
    vcfg = cfg["video"]
    video: dict[str, Any] = {"action": video_action}
    effective_dur = trim_s if trim_s is not None else (media.duration_s or 0.0)
    acfg = cfg["audio"]
    audio_kbps = float(acfg.get("bitrate_kbps", 128))
    ab_rule = rules.get("audio.bitrate", {})
    if "max_kbps" in ab_rule:
        audio_kbps = min(audio_kbps, float(ab_rule["max_kbps"]))
    if video_action == "encode":
        v = media.video
        size_ids = {"video.size", "video.resolution", "video.aspect", "video.sar"}
        size_failing = any(r.id in size_ids for r in failing)
        dw, dh = square_pixel_size(v)
        if (dw, dh) != (v.display_width, v.display_height):
            notes.append(f"non-square pixels (SAR {v.sar}) are resampled to square pixels ({dw}x{dh} before fitting)")
        if (v.color_transfer or "").lower() in HDR_TRANSFERS:
            notes.append(
                f"the source is HDR ({v.color_transfer}); it is re-encoded to 8-bit {vcfg.get('pix_fmt', 'yuv420p')} "
                "without tone mapping, so colours and highlights may look wrong. Export SDR from your editor first."
            )
        target: tuple[int, int] | None = None
        if size_failing:
            target = parse_size(cfg.get("size") or "")
            if target is None:
                size_rule = rules.get("video.size", {})
                listed = (size_rule.get("allowed") or []) + (size_rule.get("recommended") or [])
                target = parse_size(listed[0]) if listed else None
        if target is None:
            target = (_even(dw), _even(dh))
        fps_rule = rules.get("video.fps", {})
        if cfg.get("fps", "auto") != "auto":
            fps = Fraction(cfg["fps"]).limit_denominator(1001)
        else:
            fps = choose_fps(
                media.fps,
                fps_rule.get("min"),
                fps_rule.get("max"),
                fps_rule.get("recommended_min"),
                fps_rule.get("recommended_max"),
            )
        keyint_s = float(vcfg.get("keyint_s", 2))
        ki_rule = rules.get("video.keyint", {})
        if "max_s" in ki_rule and RANK[str(ki_rule.get("severity", "warn"))] >= RANK["warn"]:
            keyint_s = min(keyint_s, float(ki_rule["max_s"]))
        gop = max(1, round(keyint_s * float(fps)))
        if (dw, dh) == target:
            resize = "kept"
        elif dw and dh and abs(dw / dh - target[0] / target[1]) / (target[0] / target[1]) < 0.005:
            resize = "scaled"
        else:
            resize = str(opts.fit or cfg.get("fit", "pad"))
        video.update(
            {
                "width": target[0],
                "height": target[1],
                "resize": resize,
                "fit": opts.fit or cfg.get("fit", "pad"),
                "pad_color": cfg.get("pad_color", "black"),
                "fps": fraction_text(fps),
                "gop": gop,
                "encoder": vcfg.get("encoder", "libx264"),
                "profile": vcfg.get("profile", "high"),
                "level": str(vcfg["level"]) if vcfg.get("level") else None,
                "preset": opts.preset or vcfg.get("preset", "medium"),
                "bframes": int(vcfg.get("bframes", 0)),
                "pix_fmt": vcfg.get("pix_fmt", "yuv420p"),
                "deinterlace": (v.field_order or "").lower() in ("tt", "bb", "tb", "bt"),
            }
        )
        if vcfg.get("cbr_kbps"):
            video["rate"] = {"mode": "cbr", "kbps": float(vcfg["cbr_kbps"])}
        else:
            maxrate = vcfg.get("maxrate_kbps")
            maxrate = float(maxrate) if maxrate else None
            caps = []
            vb = rules.get("video.bitrate", {})
            if "max_kbps" in vb:
                caps.append(float(vb["max_kbps"]) * 0.95)
            tb = rules.get("bitrate.total", {})
            if "max_kbps" in tb:
                caps.append((float(tb["max_kbps"]) - audio_kbps) * 0.95)
            fs = rules.get("file.size", {})
            if "max_mb" in fs and effective_dur > 0:
                caps.append(float(fs["max_mb"]) * 8000.0 / effective_dur * 0.92 - audio_kbps)
            if caps:
                cap = min(caps)
                maxrate = min(maxrate, cap) if maxrate else cap
            if maxrate is not None and maxrate < 250:
                blockers.append(
                    f"the video cannot fit the size/bitrate limits at a usable quality (cap {maxrate:.0f} kbps)"
                )
            bufsize = vcfg.get("bufsize_kbps")
            bufsize = float(bufsize) if bufsize else (2 * maxrate if maxrate else None)
            if maxrate and bufsize:
                bufsize = min(bufsize, 2 * maxrate)
            video["rate"] = {
                "mode": "crf",
                "crf": float(opts.crf if opts.crf is not None else vcfg.get("crf", 20)),
                "maxrate_kbps": round(maxrate) if maxrate else None,
                "bufsize_kbps": round(bufsize) if bufsize else None,
            }

    # ------------------------------------------------ audio settings
    audio: dict[str, Any] = {"action": audio_action}
    loudness: dict[str, Any] | None = None
    if audio_action in ("encode", "silent"):
        sample_rate = int(acfg.get("sample_rate", 48000))
        sr_rule = rules.get("audio.sample_rate", {})
        if sr_rule.get("allowed") and sample_rate not in sr_rule["allowed"]:
            sample_rate = 48000 if 48000 in sr_rule["allowed"] else int(sr_rule["allowed"][0])
        if "max_hz" in sr_rule and sample_rate > sr_rule["max_hz"]:
            sample_rate = int(sr_rule["max_hz"])
        channels = int(acfg.get("channels", 2))
        ch_rule = rules.get("audio.channels", {})
        if ch_rule.get("allowed") and channels not in ch_rule["allowed"]:
            channels = 2 if 2 in ch_rule["allowed"] else int(ch_rule["allowed"][0])
        audio.update(
            {
                "encoder": acfg.get("encoder", "aac"),
                "bitrate_kbps": audio_kbps,
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
        lcfg = cfg.get("loudness")
        if audio_action == "encode" and opts.loudnorm and lcfg and "audio.loudness" in rules:
            loud_target = float(rules["audio.loudness"].get("target_lufs", lcfg.get("target_lufs", -14)))
            tp = float(lcfg.get("true_peak_dbtp", -1.5))
            if "audio.true_peak" in rules:
                tp = min(tp, float(rules["audio.true_peak"]["max_dbtp"]) - 0.5)
            loudness = {"target_lufs": loud_target, "true_peak_dbtp": tp, "lra": float(lcfg.get("lra", 11))}
    if trim_s is not None:
        notes.append(f"the output is cut to {trim_s:.2f} s with a 0.3 s audio fade-out")

    container = {
        "format": fmt,
        "faststart": bool(cfg["container"].get("faststart", True)),
        "no_edit_list": no_editlist and (video_action == "encode" or not media.video_reordered),
    }
    plan = FixPlan(
        source=media.path,
        output=output,
        profile=profile.id,
        mode=mode,
        video_action=video_action,
        audio_action=audio_action,
        reasons={k: v for k, v in reasons.items() if v},
        unfixed=unfixed,
        blockers=blockers,
        notes=notes,
        video=video,
        audio=audio,
        loudness=loudness,
        container=container,
        strip_metadata=bool(cfg.get("strip_metadata", True)) and not opts.keep_metadata,
        trim_s=trim_s,
    )
    plan.recipe = sha256_json(
        {
            "tool": __version__,
            "profile": profile.digest,
            "mode": mode,
            "video": video,
            "audio": audio,
            "loudness": loudness,
            "container": container,
            "strip_metadata": plan.strip_metadata,
            "trim_s": trim_s,
        }
    )[:16]
    return plan


# ---------------------------------------------------------------------- commands
def video_filter(plan: FixPlan, media: MediaInfo) -> str:
    """The ``-filter_complex`` graph for a video re-encode (output label ``[v]``)."""
    assert media.video is not None
    v = plan.video
    w, h = int(v["width"]), int(v["height"])
    src = f"[0:{media.video.index}]"
    pre = "bwdif=mode=send_frame:parity=auto:deint=interlaced," if v.get("deinterlace") else ""
    dw, dh = square_pixel_size(media.video)
    if (dw, dh) != (media.video.display_width, media.video.display_height):
        # Resample anamorphic pixels first; the final setsar=1 alone would only relabel them and distort the picture.
        # FFmpeg has already applied any rotation (autorotate), so these are display-oriented dimensions.
        pre += f"scale={dw}:{dh}:flags=lanczos,setsar=1,"
    tail_chain = f"fps={v['fps']},format={v['pix_fmt']},setsar=1"
    same_aspect = dw and dh and abs((dw / dh) - (w / h)) / (w / h) < 0.005
    if (dw, dh) == (w, h):
        return f"{src}{pre}{tail_chain}[v]"
    if same_aspect:
        return f"{src}{pre}scale={w}:{h}:flags=lanczos,{tail_chain}[v]"
    fit = v.get("fit", "pad")
    if fit == "crop":
        return (
            f"{src}{pre}scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},{tail_chain}[v]"
        )
    if fit == "blur":
        bw, bh = _even(w / 8), _even(h / 8)
        return (
            f"{src}{pre}split=2[bg][fg];"
            f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,scale={w}:{h}[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,{tail_chain}[v]"
        )
    color = v.get("pad_color", "black")
    return (
        f"{src}{pre}scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={color},{tail_chain}[v]"
    )


#: Stand-in for first-pass measurements when a dry run previews the commands.
PREVIEW_MEASUREMENTS = {
    "input_i": "<pass1-I>",
    "input_tp": "<pass1-TP>",
    "input_lra": "<pass1-LRA>",
    "input_thresh": "<pass1-thresh>",
    "target_offset": "<pass1-offset>",
}


def loudnorm_filter(plan: FixPlan, measured: dict[str, str] | None) -> str | None:
    """Second-pass loudnorm filter from first-pass measurements (``None`` = do not normalise)."""
    if plan.loudness is None or measured is None:
        return None
    if measured is PREVIEW_MEASUREMENTS:
        lo = plan.loudness
        return (
            f"loudnorm=I={fmt_num(lo['target_lufs'])}:TP={fmt_num(lo['true_peak_dbtp'])}:LRA={fmt_num(lo['lra'])}"
            + "".join(
                f":{k}={v}"
                for k, v in zip(
                    ("measured_I", "measured_TP", "measured_LRA", "measured_thresh", "offset"),
                    measured.values(),
                    strict=True,
                )
            )
            + ":linear=true:print_format=none"
        )
    try:
        mi = float(measured["input_i"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(mi) or mi < -60:
        return None  # (near) silence: normalising would only amplify noise
    # Every value is parsed as a number and re-formatted, so text from FFmpeg's log can never
    # change the filtergraph.
    values: dict[str, float] = {}
    for key, default in (
        ("input_i", None),
        ("input_tp", None),
        ("input_lra", "0"),
        ("input_thresh", None),
        ("target_offset", "0"),
    ):
        raw = measured.get(key, default)
        try:
            number = float(raw) if raw is not None else math.nan
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise FixError(f"unexpected loudnorm measurement {key}={raw!r}")
        values[key] = number
    lo = plan.loudness
    lra = float(lo["lra"])
    if values["input_lra"] > lra:
        lra = min(20.0, values["input_lra"] + 1.0)  # keep linear mode possible for dynamic material
    return (
        f"loudnorm=I={fmt_num(lo['target_lufs'])}:TP={fmt_num(lo['true_peak_dbtp'])}:LRA={fmt_num(lra)}"
        f":measured_I={fmt_num(values['input_i'])}:measured_TP={fmt_num(values['input_tp'])}"
        f":measured_LRA={fmt_num(values['input_lra'])}:measured_thresh={fmt_num(values['input_thresh'])}"
        f":offset={fmt_num(values['target_offset'])}:linear=true:print_format=none"
    )


def _audio_chain(plan: FixPlan, measured: dict[str, str] | None) -> str | None:
    parts = []
    ln = loudnorm_filter(plan, measured)
    if ln:
        parts.append(ln)
    if plan.audio_action == "encode":
        parts.append(f"aresample={plan.audio['sample_rate']}")
    if plan.trim_s is not None and plan.trim_s > 0.6:
        parts.append(f"afade=t=out:st={plan.trim_s - 0.3:.3f}:d=0.3")
    return ",".join(parts) if parts else None


def _audio_encode_args(plan: FixPlan, bitrate_kbps: float) -> list[str]:
    a = plan.audio
    return ["-c:a", "aac", "-b:a", f"{int(bitrate_kbps)}k", "-ar", str(a["sample_rate"]), "-ac", str(a["channels"])]


def _mux_args(plan: FixPlan, media: MediaInfo, out_path: str) -> list[str]:
    args: list[str] = []
    if plan.trim_s is not None:
        args += ["-t", f"{plan.trim_s:.3f}"]
    if plan.strip_metadata:
        args += ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
    if plan.container["no_edit_list"]:
        args += ["-use_editlist", "0"]
    if plan.container["faststart"]:
        args += ["-movflags", "+faststart"]
    args += ["-max_muxing_queue_size", "4096", "-f", plan.container["format"], out_path]
    return args


def _video_copy_args(media: MediaInfo, *, input_index: int = 0, stream: str | None = None) -> list[str]:
    assert media.video is not None
    spec = stream if stream is not None else f"{input_index}:{media.video.index}"
    args = ["-map", spec, "-c:v", "copy"]
    if (media.video.codec or "") == "hevc":
        args += ["-tag:v", "hvc1"]  # Apple players need hvc1, not hev1
    return args


def build_encode_args(
    plan: FixPlan,
    media: MediaInfo,
    runner: Runner,
    out_path: str,
    measured: dict[str, str] | None = None,
    *,
    audio_bitrate_kbps: float | None = None,
) -> list[str]:
    """The main ffmpeg command for ``plan`` (remux, audio or full mode)."""
    assert media.video is not None
    t = str(runner.threads)
    args = [runner.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", t]
    args += hardened_input(plan.source)
    if plan.audio_action == "silent":
        dur = plan.trim_s or media.duration_s or 1.0
        layout = "stereo" if int(plan.audio["channels"]) == 2 else "mono"
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={plan.audio['sample_rate']}:cl={layout}"]
    # video
    if plan.video_action == "encode":
        v = plan.video
        args += ["-filter_complex", video_filter(plan, media), "-filter_complex_threads", t, "-map", "[v]"]
        args += ["-c:v", "libx264", "-preset", str(v["preset"]), "-profile:v", str(v["profile"])]
        if v.get("level"):
            args += ["-level:v", str(v["level"])]
        args += ["-pix_fmt", str(v["pix_fmt"]), "-bf", str(v["bframes"]), "-g", str(v["gop"]), "-flags", "+cgop"]
        rate = v["rate"]
        if rate["mode"] == "cbr":
            k = f"{int(rate['kbps'])}k"
            args += ["-b:v", k, "-minrate", k, "-maxrate", k, "-bufsize", k, "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        else:
            args += ["-crf", fmt_num(rate["crf"])]
            if rate.get("maxrate_kbps"):
                args += ["-maxrate", f"{rate['maxrate_kbps']}k", "-bufsize", f"{rate['bufsize_kbps']}k"]
        args += ["-threads", t]
    else:
        args += _video_copy_args(media)
    # audio
    bitrate = audio_bitrate_kbps or float(plan.audio.get("bitrate_kbps", 128))
    if plan.audio_action == "copy":
        assert media.audio is not None
        args += ["-map", f"0:{media.audio.index}", "-c:a", "copy"]
    elif plan.audio_action == "encode":
        assert media.audio is not None
        args += ["-map", f"0:{media.audio.index}"]
        chain = _audio_chain(plan, measured)
        if chain:
            args += ["-af", chain]
        args += _audio_encode_args(plan, bitrate)
    elif plan.audio_action == "silent":
        args += ["-map", "1:a:0", *_audio_encode_args(plan, bitrate)]
    else:
        args += ["-an"]
    args += ["-sn", "-dn"]
    return args + _mux_args(plan, media, out_path)


def build_audio_redo_args(
    plan: FixPlan,
    media: MediaInfo,
    runner: Runner,
    *,
    encoded: str,
    out_path: str,
    measured: dict[str, str] | None,
    bitrate_kbps: float,
) -> list[str]:
    """Re-encode only the audio (from the source, to avoid generation loss) at a lower bitrate."""
    assert media.audio is not None
    args = [runner.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", str(runner.threads)]
    args += [*hardened_input(encoded), *hardened_input(plan.source)]
    args += ["-map", "0:v:0", "-c:v", "copy"]
    if (media.video.codec if media.video else "") == "hevc" and plan.video_action == "copy":
        args += ["-tag:v", "hvc1"]
    args += ["-map", f"1:{media.audio.index}"]
    chain = _audio_chain(plan, measured)
    if chain:
        args += ["-af", chain]
    args += [*_audio_encode_args(plan, bitrate_kbps), "-sn", "-dn"]
    return args + _mux_args(plan, media, out_path)


def build_loudnorm_measure_args(plan: FixPlan, media: MediaInfo, runner: Runner) -> list[str]:
    assert media.audio is not None and plan.loudness is not None
    lo = plan.loudness
    args = [runner.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-threads", str(runner.threads)]
    args += hardened_input(plan.source)
    args += ["-map", f"0:{media.audio.index}", "-vn", "-sn", "-dn"]
    if plan.trim_s is not None:
        args += ["-t", f"{plan.trim_s:.3f}"]
    flt = (
        f"loudnorm=I={fmt_num(lo['target_lufs'])}:TP={fmt_num(lo['true_peak_dbtp'])}"
        f":LRA={fmt_num(lo['lra'])}:print_format=json"
    )
    return [*args, "-af", flt, "-f", "null", "-"]


# ---------------------------------------------------------------------- manifest
class Manifest:
    """Journal of fixes written into one output directory (``.svqa-manifest.json``)."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.path = os.path.join(directory, MANIFEST_NAME)
        self.lock_path = os.path.join(directory, LOCK_NAME)

    def load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"schema": MANIFEST_SCHEMA, "entries": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise FixError(f"{self.path} is unreadable ({exc}); move it away to start a fresh journal") from exc
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            raise FixError(f"{self.path} is not a social-video-qa manifest; move it away to start a fresh journal")
        return data

    def entry(self, name: str) -> dict[str, Any] | None:
        entry = self.load()["entries"].get(name)
        return entry if isinstance(entry, dict) else None

    def put(self, name: str, entry: dict[str, Any]) -> None:
        data = self.load()
        data["schema"] = MANIFEST_SCHEMA
        data["entries"][name] = entry
        # Normal file permissions (not mkstemp's 0600), so others who share the folder can read the journal.
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=_umask_mode())

    def outputs(self) -> set[str]:
        try:
            return set(self.load()["entries"])
        except FixError:
            return set()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock so two runs never write the same directory at once."""
        for attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError as exc:
                if attempt == 0 and self._stale():
                    with contextlib.suppress(OSError):
                        os.unlink(self.lock_path)
                    continue
                raise FixError(
                    f"another `svqa fix` run is writing to {self.directory} ({self.lock_path} exists). "
                    "If no other run is active, delete the lock file."
                ) from exc
        else:  # unreachable: every attempt either breaks with the lock or raises
            raise FixError(f"could not take the lock {self.lock_path}")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}))
            yield
        finally:
            with contextlib.suppress(OSError):
                os.unlink(self.lock_path)

    def _stale(self) -> bool:
        try:
            with open(self.lock_path, encoding="utf-8") as f:
                pid = int(json.load(f).get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pid = 0
        if os.name == "posix" and pid > 0:
            try:
                os.kill(pid, 0)  # signal 0: existence test only
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return False
        try:  # elsewhere: treat locks older than six hours as stale
            return time.time() - os.path.getmtime(self.lock_path) > 6 * 3600
        except OSError:
            return True


# ---------------------------------------------------------------------- execution
def _run(runner: Runner, args: list[str], what: str) -> str:
    proc = runner.run(args, timeout=None, check=False)
    if proc.returncode != 0:
        raise FixError(f"{what} failed: {tail(proc.stderr, 1200)}")
    return proc.stderr


def _cleanup(path: str | None) -> None:
    if path:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _verify(
    path: str, display: str, profile: Profile, runner: Runner, opts: FixOptions
) -> tuple[MediaInfo, CheckReport]:
    needs = needs_for(profile.rules, opts.skip, opts.only)
    media = analyze(path, runner, needs, start_window_s=window_for(profile.rules))
    report = run_checks(
        media,
        profile.rules,
        file=display,
        profile=profile.id,
        profile_title=profile.title,
        skip=opts.skip,
        only=opts.only,
        strict=opts.strict,
    )
    return media, report


def fix_file(
    source: str,
    profile: Profile,
    runner: Runner,
    opts: FixOptions,
    *,
    media: MediaInfo | None = None,
    log: Any = None,
) -> FixResult:
    """Plan (and with ``opts.apply``, perform) the fix for one file."""
    t0 = time.monotonic()

    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    fmt = output_format(profile)
    output = default_output(source, profile.id, opts, fmt)
    if _same_file(source, output):
        return FixResult(source, output, "refused", "output would overwrite the input; choose another --output")
    try:
        if media is None:
            needs = needs_for(profile.rules, opts.skip, opts.only)
            media = analyze(source, runner, needs, start_window_s=window_for(profile.rules))
    except ProbeError as exc:
        return FixResult(source, output, "error", str(exc))
    before = run_checks(
        media,
        profile.rules,
        file=source,
        profile=profile.id,
        profile_title=profile.title,
        skip=opts.skip,
        only=opts.only,
        strict=opts.strict,
    )
    try:
        plan = plan_fix(media, before, profile, opts, output)
    except FixError as exc:
        return FixResult(source, output, "error", str(exc), before=before)
    missing = missing_encoders(plan, runner)
    if missing:  # say so in the dry run already, not only when --apply fails
        plan.notes.append(f"this FFmpeg has no {' or '.join(missing)} encoder: --apply will fail (run svqa doctor)")
    preview = [build_encode_args(plan, media, runner, output, PREVIEW_MEASUREMENTS)] if plan.mode != "none" else []
    if plan.loudness is not None and plan.audio_action == "encode":
        preview.insert(0, build_loudnorm_measure_args(plan, media, runner))
    result = FixResult(source, output, "planned", plan=plan, before=before, commands=preview)

    if plan.blockers:
        result.status = "refused"
        result.message = "cannot produce a passing file: " + "; ".join(plan.blockers)
        return result
    if plan.mode == "none":
        result.status = "nothing-to-do"
        result.message = "already passes every fixable check"
        if plan.unfixed:
            result.message += "; not fixable automatically: " + ", ".join(r.id for r in plan.unfixed)
        return result

    out_dir = os.path.dirname(os.path.abspath(output))
    manifest = Manifest(out_dir) if opts.manifest else None
    name = os.path.basename(output)
    src_sha: str | None = None
    # While an output we wrote stays published, its journal entry must keep proving that we wrote it,
    # even if this run fails or is interrupted; otherwise the next run would need --force.
    prev_out: dict[str, Any] = {}
    if os.path.exists(output):
        try:
            src_sha = sha256_file(source)
            entry = manifest.entry(name) if manifest and os.path.isdir(out_dir) else None
            out_sha = sha256_file(output)
            out_size = os.path.getsize(output)
        except (FixError, OSError) as exc:
            result.status = "error"
            result.message = str(exc)
            return result
        ours = entry is not None and entry.get("output_sha256") == out_sha
        if ours and entry is not None:
            prev_out = {"output_sha256": out_sha, "output_size": out_size}
            other_source = entry.get("source_path_sha256")
            if (
                other_source
                and other_source != source_path_key(source)
                and entry.get("source_sha256") != src_sha
                and not opts.force
            ):
                result.status = "refused"
                result.message = (
                    f"{output} was written from another source ({entry.get('source')}); "
                    "use --force to replace it, or choose another --out-dir"
                )
                return result
            if (
                entry.get("status") == "ok"
                and entry.get("source_sha256") == src_sha
                and entry.get("recipe") == plan.recipe
                and not opts.force
            ):
                warned = [
                    cid
                    for cid in (entry.get("verification") or {}).get("warned_checks") or []
                    if cid in profile.rules
                    and effective_severity(cid, profile.rules[cid], opts.skip, opts.only) != "off"
                ]
                if opts.strict and warned:
                    result.status = "failed"
                    result.message = (
                        f"{output} is up to date, but its verification warned about {', '.join(warned)}, "
                        "which --strict treats as failures"
                    )
                    return result
                result.status = "up-to-date"
                result.message = f"{output} is up to date (same source and recipe)"
                return result
        if not ours and not opts.force:
            result.status = "refused"
            result.message = (
                f"{output} exists and was not written by this tool (or was modified); use --force to replace it"
            )
            return result

    if not opts.apply:
        result.message = "dry run: nothing was written"
        return result

    # ------------------------------------------------ apply
    tmp: str | None = None
    tmp2: str | None = None
    try:
        os.makedirs(out_dir, exist_ok=True)
        with manifest.lock() if manifest else contextlib.nullcontext():
            entry = {
                "status": "encoding",
                "source": source,
                "source_path_sha256": source_path_key(source),
                "source_sha256": src_sha or sha256_file(source),
                "source_size": media.size_bytes,
                "profile": profile.id,
                "profile_sha256": profile.digest,
                "recipe": plan.recipe,
                "mode": plan.mode,
                "tool_version": __version__,
                "ffmpeg_version": runner.version_label(),
                "started_at": utc_now(),
                **prev_out,
            }
            try:
                if manifest:
                    manifest.put(name, entry)  # the intent is journaled before any work starts
                # Random, exclusively created temp names: a planted symlink cannot redirect FFmpeg's output.
                tmp = _private_temp(out_dir, name, fmt)
                tmp2 = _private_temp(out_dir, name, fmt)
                measured = None
                if plan.loudness is not None and plan.audio_action == "encode":
                    say("  measuring loudness (loudnorm pass 1)")
                    measured = parse_loudnorm_json(
                        _run(runner, build_loudnorm_measure_args(plan, media, runner), "loudnorm pass 1")
                    )
                    if measured is None:
                        raise FixError("loudnorm pass 1 printed no measurements")
                    if loudnorm_filter(plan, measured) is None:
                        plan.notes.append("the audio is (near) silent, so loudness normalisation was skipped")
                action = {"full": "re-encoding", "remux": "remuxing"}.get(plan.mode, "re-encoding audio")
                say(f"  {action} -> {output}")
                args = build_encode_args(plan, media, runner, tmp, measured)
                result.commands = [args]
                _run(runner, args, "ffmpeg")
                # AAC sometimes lands a few kbps above the requested bitrate; step down if a ceiling applies.
                max_ab = profile.rules.get("audio.bitrate", {}).get("max_kbps")
                if max_ab and plan.audio_action == "encode":
                    bitrate = float(plan.audio["bitrate_kbps"])
                    for _attempt in range(2):
                        probe = analyze(tmp, runner, needs=frozenset())
                        measured_ab = probe.audio.bitrate_kbps if probe.audio else None
                        if not measured_ab or measured_ab <= float(max_ab):
                            break
                        bitrate = max(32.0, math.floor(bitrate * 0.875 / 8) * 8)
                        say(f"  audio bitrate {measured_ab:.0f} kbps > {max_ab} kbps: re-encoding at {bitrate:.0f}k")
                        redo = build_audio_redo_args(
                            plan, media, runner, encoded=tmp, out_path=tmp2, measured=measured, bitrate_kbps=bitrate
                        )
                        _run(runner, redo, "audio re-encode")
                        os.replace(tmp2, tmp)
                        plan.notes.append(f"audio re-encoded at {bitrate:.0f} kbps to stay under {max_ab} kbps")
                say("  verifying the output")
                _vmedia, after = _verify(tmp, output, profile, runner, opts)
                result.after = after
                if not after.ok:
                    failed_ids = ", ".join(
                        r.id for r in after.failures(("error", "warn") if opts.strict else ("error",))
                    )
                    kept = ""
                    if opts.keep_failed:
                        keep = _unique(os.path.splitext(output)[0] + ".failed." + fmt)
                        os.replace(tmp, keep)
                        kept = f"; kept for inspection as {keep}"
                    result.status = "failed"
                    result.message = f"the re-encoded file still fails: {failed_ids}{kept}"
                    _journal(
                        manifest,
                        name,
                        {**entry, "status": "failed", "finished_at": utc_now(), "failed_checks": failed_ids},
                    )
                    return result
                os.replace(tmp, output)
                with contextlib.suppress(OSError):
                    os.chmod(output, _umask_mode())  # mkstemp made it 0600; give it normal file permissions
                # From here on the published file is the new one, whatever happens to the journal.
                entry.update(output_sha256=sha256_file(output), output_size=os.path.getsize(output))
                result.status = "written"
                result.message = f"wrote {output}"
                problem = _journal(
                    manifest,
                    name,
                    {
                        **entry,
                        "status": "ok",
                        "finished_at": utc_now(),
                        "verification": {
                            "errors": after.counts["error"],
                            "warnings": after.counts["warn"],
                            "warned_checks": [r.id for r in after.failures(("warn",))],
                        },
                    },
                )
                if problem:
                    result.message += f" ({problem})"
                return result
            except (FixError, FFmpegError, ProbeError, OSError) as exc:
                result.status = "error"
                result.message = str(exc)
                _journal(
                    manifest, name, {**entry, "status": "failed", "finished_at": utc_now(), "error": str(exc)[:500]}
                )
                return result
            except BaseException:
                with contextlib.suppress(Exception):
                    _journal(manifest, name, {**entry, "status": "interrupted", "finished_at": utc_now()})
                raise
            finally:
                _cleanup(tmp)
                _cleanup(tmp2)
    except (FixError, OSError) as exc:  # the output folder could not be created or locked
        result.status = "error"
        result.message = str(exc)
        return result
    finally:
        result.elapsed_s = time.monotonic() - t0


def missing_encoders(plan: FixPlan, runner: Runner) -> list[str]:
    """Encoders ``plan`` writes with that this FFmpeg lacks (none when its encoder list cannot be read)."""
    needed = ["libx264"] if plan.video_action == "encode" else []
    if plan.audio_action in ("encode", "silent"):
        needed.append("aac")
    have = runner.encoder_names() if needed else set()
    return [name for name in needed if have and name not in have]


def _journal(manifest: Manifest | None, name: str, entry: dict[str, Any]) -> str | None:
    """Record ``entry`` in the manifest; returns a description of the problem instead of raising."""
    if manifest is None:
        return None
    try:
        manifest.put(name, entry)
    except (FixError, OSError) as exc:
        return f"the manifest could not be updated: {exc}"
    return None


def _umask_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _private_temp(directory: str, name: str, ext: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f".{name}.svqa-tmp-", suffix=f".{ext}", dir=directory)
    os.close(fd)
    return path


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


_RESIZE_TEXT = {
    "kept": "size kept",
    "scaled": "scaled",
    "pad": "scaled and padded",
    "crop": "scaled and cropped",
    "blur": "scaled onto a blurred fill",
}


def describe_plan(plan: FixPlan) -> list[str]:
    """Human description of what a plan will do."""
    lines: list[str] = []
    v, a = plan.video, plan.audio
    if plan.video_action == "encode":
        rate = v["rate"]
        if rate["mode"] == "cbr":
            rate_text = f"CBR {rate['kbps'] / 1000:g} Mbps"
        else:
            rate_text = f"CRF {fmt_num(rate['crf'])}" + (
                f", max {rate['maxrate_kbps']} kbps" if rate.get("maxrate_kbps") else ""
            )
        level = f" level {v['level']}" if v.get("level") else ""
        bframes = "no B-frames" if not v["bframes"] else f"{v['bframes']} B-frames"
        deint = ", de-interlace" if v.get("deinterlace") else ""
        lines.append(
            f"video: re-encode with libx264 {v['profile']}{level}, {rate_text}, preset {v['preset']}, "
            f"{bframes}, closed GOP every {v['gop']} frames, "
            f"{v['width']}x{v['height']} ({_RESIZE_TEXT.get(v['resize'], v['resize'])}), {v['fps']} fps CFR{deint}"
        )
    else:
        lines.append("video: copy (no re-encode)")
    if plan.audio_action == "encode":
        loud = ""
        if plan.loudness:
            lo = plan.loudness
            loud = f", two-pass loudnorm to {fmt_num(lo['target_lufs'])} LUFS / {fmt_num(lo['true_peak_dbtp'])} dBTP"
        lines.append(
            f"audio: re-encode AAC {a['bitrate_kbps']:.0f} kbps, {a['sample_rate']} Hz, {a['channels']} ch{loud}"
        )
    elif plan.audio_action == "silent":
        lines.append(f"audio: add a silent AAC track ({a['sample_rate']} Hz, {a['channels']} ch)")
    elif plan.audio_action == "copy":
        lines.append("audio: copy (no re-encode)")
    else:
        lines.append("audio: none")
    c = plan.container
    mux = [c["format"].upper(), "moov first" if c["faststart"] else "moov last"]
    if c["no_edit_list"]:
        mux.append("no edit list")
    mux.append("metadata stripped" if plan.strip_metadata else "metadata kept")
    if plan.trim_s is not None:
        mux.append(f"cut to {plan.trim_s:.2f} s")
    lines.append("container: " + ", ".join(mux))
    return lines
