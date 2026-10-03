"""Render check reports as human text, JSON or JUnit XML, and profiles as text or Markdown."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from . import __version__
from .checks import COMMON_PARAMS, REGISTRY, CheckReport, Result, describe_rule
from .hints import hint_for
from .probe import MediaInfo
from .profiles import Profile
from .util import fmt_bytes, fmt_kbps, fmt_num, ratio_label, round_floats

SCHEMA = "social-video-qa/report@1"


# ---------------------------------------------------------------------- styling
@dataclass
class Style:
    color: bool = False
    unicode: bool = True

    @classmethod
    def for_stream(cls, stream: TextIO, mode: str = "auto") -> Style:
        encoding = (getattr(stream, "encoding", None) or "").lower()
        uni = "utf" in encoding
        if mode == "always":
            return cls(color=True, unicode=uni)
        if mode == "never" or os.environ.get("NO_COLOR"):
            return cls(color=False, unicode=uni)
        tty = hasattr(stream, "isatty") and stream.isatty()
        return cls(color=bool(tty and os.environ.get("TERM") != "dumb"), unicode=uni)

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def mark(self, kind: str) -> str:
        glyphs = {
            "pass": ("✓", "PASS", "32"),
            "error": ("✗", "FAIL", "31"),
            "warn": ("!", "WARN", "33"),
            "info": ("i", "INFO", "36"),
            "skip": ("-", "SKIP", "2"),
        }
        uni, ascii_, code = glyphs[kind]
        return self.paint(uni if self.unicode else ascii_, code)

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")


def _kind(r: Result) -> str:
    if r.status == "pass":
        return "pass"
    if r.status == "skip":
        return "skip"
    return r.level or "error"


# ---------------------------------------------------------------------- media summary
def media_summary(m: MediaInfo) -> list[tuple[str, str]]:
    """Compact one-line descriptions of the video, audio and container."""
    rows: list[tuple[str, str]] = []
    v = m.video
    if v is not None:
        parts = [f"{v.codec or '?'}" + (f" {v.profile}" if v.profile else "") + (f"@{v.level:.1f}" if v.level else "")]
        parts.append(f"{v.display_width}x{v.display_height}" + (f" (rot {v.rotation})" if v.rotation else ""))
        if m.fps:
            cfr = ""
            if m.packets is not None:
                bad, considered, _w = m.packets.irregular()
                cfr = " CFR" if considered and not bad else (" VFR" if considered else "")
            parts.append(f"{fmt_num(m.fps, 3)} fps{cfr}")
        if v.bitrate_kbps:
            parts.append(fmt_kbps(v.bitrate_kbps))
        if v.pix_fmt:
            parts.append(v.pix_fmt)
        if m.packets is not None:
            parts.append("B-frames" if m.packets.reordered else "no B-frames")
        rows.append(("video", " · ".join(parts)))
    else:
        rows.append(("video", "none"))
    a = m.audio
    if a is not None:
        parts = [f"{a.codec or '?'}" + (f" {a.profile}" if a.profile else "")]
        if a.sample_rate:
            parts.append(f"{fmt_num(a.sample_rate / 1000, 1)} kHz")
        if a.channels:
            parts.append(f"{a.channels} ch")
        if a.bitrate_kbps:
            parts.append(fmt_kbps(a.bitrate_kbps))
        if m.loudness is not None:
            if m.loudness.silent:
                parts.append("silent")
            else:
                parts.append(f"{fmt_num(m.loudness.integrated_lufs, 1)} LUFS")
                if m.loudness.true_peak_dbtp is not None:
                    parts.append(f"TP {fmt_num(m.loudness.true_peak_dbtp, 1)} dBTP")
        if m.hiss_lufs is not None and not (m.loudness and m.loudness.silent):
            parts.append(f"hiss {fmt_num(m.hiss_lufs, 1)}")
        rows.append(("audio", " · ".join(parts)))
    else:
        rows.append(("audio", "none"))
    parts = [str(m.container or "?")]
    if m.duration_s is not None:
        parts.append(f"{m.duration_s:.2f} s")
    parts.append(fmt_bytes(m.size_bytes))
    if m.bitrate_kbps:
        parts.append(fmt_kbps(m.bitrate_kbps))
    if m.boxes is not None and m.boxes.is_iso:
        if m.boxes.moov_before_mdat is not None:
            parts.append("moov first" if m.boxes.moov_before_mdat else "moov last")
        n = len(m.boxes.tracks_with_edit_lists)
        parts.append(f"{n} edit list(s)" if n else "no edit lists")
    if m.video is not None and m.video.display_aspect:
        parts.append(ratio_label(m.video.display_aspect))
    rows.append(("file", " · ".join(parts)))
    return rows


# ---------------------------------------------------------------------- human
def _count_text(report: CheckReport) -> str:
    c = report.counts
    bits = []
    if c["error"]:
        bits.append(f"{c['error']} error{'s' if c['error'] != 1 else ''}")
    if c["warn"]:
        bits.append(f"{c['warn']} warning{'s' if c['warn'] != 1 else ''}")
    if c["info"]:
        bits.append(f"{c['info']} note{'s' if c['info'] != 1 else ''}")
    return ", ".join(bits)


def _status_word(report: CheckReport, style: Style) -> str:
    if report.error:
        return style.paint("ERROR", "31;1")
    if not report.ok:
        return style.paint("FAIL", "31;1")
    if report.counts["warn"]:
        return style.paint("PASS with warnings", "33;1")
    return style.paint("PASS", "32;1")


def render_human(
    reports: Sequence[CheckReport],
    media: dict[str, MediaInfo],
    *,
    style: Style | None = None,
    verbose: bool = False,
    quiet: bool = False,
    show_media: bool = True,
    profile_dirs: Sequence[str] = (),
) -> str:
    """Human-readable report grouped by file.

    ``profile_dirs`` are the ``--profile-dir`` values of the run, repeated in the suggested ``svqa fix`` command.
    """
    style = style or Style()
    lines: list[str] = []
    by_file: dict[str, list[CheckReport]] = {}
    for rep in reports:
        by_file.setdefault(rep.file, []).append(rep)
    for file, reps in by_file.items():
        if quiet and all(r.ok and not r.counts["warn"] for r in reps):
            continue
        multi = len(reps) > 1
        m = media.get(file)
        if multi:
            lines.append(style.bold(file))
        compact_error: str | None = None
        hinted: set[str] = set()  # one failed analysis fails several checks: print its hint once per file
        for rep in reps:
            counts = _count_text(rep)
            header = f"{rep.file} · {rep.profile} · {_status_word(rep, style)}"
            if multi:
                header = f"  {_mark_for_report(rep, style)} {rep.profile:<28} {_status_word(rep, style)}"
            if counts:
                header += f" ({counts})"
            if multi and not verbose:
                failing = [r for r in rep.results if r.failed and r.level in ("error", "warn")]
                if rep.error:
                    header += f": {rep.error}"
                    compact_error = compact_error or rep.error
                elif failing:
                    header += style.dim(": " + ", ".join(r.id for r in failing))
                lines.append(header)
                continue
            lines.append(style.bold(header) if not multi else header)
            indent = "    " if multi else "  "
            if rep.error:
                lines.append(f"{indent}{style.mark('error')} {rep.error}")
                hint = hint_for(rep.error)
                if hint and hint not in hinted:
                    hinted.add(hint)
                    lines.append(f"{indent}  {style.dim('hint: ' + hint)}")
                continue
            if show_media and m is not None and not (multi and rep is not reps[0]):
                for label, text in media_summary(m):
                    lines.append(f"{indent}{style.dim(f'{label:<6}')}{text}")
            shown = [
                r
                for r in rep.results
                if (verbose and r.message != "disabled") or (r.failed and (r.level != "info" or not quiet))
            ]
            width = max((len(r.id) for r in shown), default=0)
            for r in shown:
                lines.append(f"{indent}{style.mark(_kind(r))} {r.id:<{width}}  {r.message}")
                if r.failed and r.note:
                    lines.append(f"{indent}  {' ' * width}  {style.dim('note: ' + r.note)}")
                cause = hint_for(r.message) if r.status == "error" else None
                if cause and cause not in hinted:
                    hinted.add(cause)
                    lines.append(f"{indent}  {' ' * width}  {style.dim('hint: ' + cause)}")
            blocking = rep.failures(("error", "warn") if rep.strict else ("error",))
            if blocking and not quiet and any(r.fixable for r in blocking):
                lines.append(f"{indent}{style.dim('fix:')} {fix_command(rep, profile_dirs)}")
        hint = hint_for(compact_error)
        if hint:
            lines.append(f"  {style.dim('hint: ' + hint)}")
        lines.append("")
    lines.append(summary_line(reports))
    return "\n".join(lines).rstrip() + "\n"


def _mark_for_report(rep: CheckReport, style: Style) -> str:
    if rep.error or not rep.ok:
        return style.mark("error")
    if rep.counts["warn"]:
        return style.mark("warn")
    return style.mark("pass")


def _quote(path: str) -> str:
    if path and all(c.isalnum() or c in "._-/+" for c in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def fix_command(rep: CheckReport, profile_dirs: Sequence[str] = ()) -> str:
    """The ``svqa fix`` command (a dry run) that plans the repair of ``rep``'s file.

    It names the profile the way the user did (an ID, or the path of a profile file) and repeats the
    ``--profile-dir`` options, so the command works when copied.
    """
    parts = ["svqa", "fix", _quote(rep.file), "-p", _quote(rep.profile_ref or rep.profile)]
    for directory in profile_dirs:
        parts += ["--profile-dir", _quote(directory)]
    return " ".join(parts)


def summary_line(reports: Sequence[CheckReport]) -> str:
    files = len({r.file for r in reports})
    profiles = len({r.profile for r in reports})
    passed = sum(1 for r in reports if r.ok)
    failed = len(reports) - passed
    warned = sum(1 for r in reports if r.ok and r.counts["warn"])
    errors = sum(r.counts["error"] for r in reports) + sum(1 for r in reports if r.error)
    warnings = sum(r.counts["warn"] for r in reports)
    head = f"{files} file{'s' if files != 1 else ''} x {profiles} profile{'s' if profiles != 1 else ''}: "
    tail = f"{passed} passed ({warned} with warnings), {failed} failed"
    return (
        head + tail + f" · {errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}"
    )


# ---------------------------------------------------------------------- JSON
def render_json(
    reports: Sequence[CheckReport],
    media: dict[str, MediaInfo],
    *,
    ffmpeg_version: str | None = None,
    strict: bool = False,
    include_media: bool = True,
) -> str:
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": {"name": "social-video-qa", "version": __version__},
        "ffmpeg": {"version": ffmpeg_version},
        "strict": strict,
        "ok": all(r.ok for r in reports),
        "results": [r.to_dict() for r in reports],
        "summary": {
            "files": len({r.file for r in reports}),
            "profiles": sorted({r.profile for r in reports}),
            "passed": sum(1 for r in reports if r.ok),
            "failed": sum(1 for r in reports if not r.ok),
            "errors": sum(r.counts["error"] for r in reports) + sum(1 for r in reports if r.error),
            "warnings": sum(r.counts["warn"] for r in reports),
        },
    }
    if include_media:
        doc["media"] = {path: m.to_dict() for path, m in media.items()}
    return json.dumps(round_floats(doc), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


# ---------------------------------------------------------------------- JUnit
def render_junit(reports: Sequence[CheckReport], *, strict: bool = False) -> str:
    """JUnit XML: one test suite per (file, profile), one test case per check."""
    root = ET.Element("testsuites", name="social-video-qa")
    total = failures = errors = skipped = 0
    for rep in reports:
        suite = ET.SubElement(root, "testsuite", name=f"{rep.file} [{rep.profile}]")
        props = ET.SubElement(suite, "properties")
        ET.SubElement(props, "property", name="profile", value=rep.profile)
        ET.SubElement(props, "property", name="tool_version", value=__version__)
        s_tests = s_fail = s_err = s_skip = 0
        classname = f"{rep.profile}.{rep.file}"
        if rep.error:
            case = ET.SubElement(suite, "testcase", classname=classname, name="analyse")
            hint = hint_for(rep.error)
            text = rep.error + (f"\nhint: {hint}" if hint else "")
            ET.SubElement(case, "error", message=rep.error, type="analysis").text = text
            s_tests += 1
            s_err += 1
        for r in rep.results:
            case = ET.SubElement(suite, "testcase", classname=classname, name=r.id)
            s_tests += 1
            if r.status == "skip":
                ET.SubElement(case, "skipped", message=r.message)
                s_skip += 1
            elif r.status == "error" and (r.level == "error" or (strict and r.level == "warn")):
                ET.SubElement(case, "error", message=r.message, type=r.level or "error").text = _detail(r)
                s_err += 1
            elif r.failed and (r.level == "error" or (strict and r.level == "warn")):
                ET.SubElement(case, "failure", message=r.message, type=r.level or "error").text = _detail(r)
                s_fail += 1
            elif r.failed:
                ET.SubElement(case, "system-out").text = f"{(r.level or '').upper()}: {r.message}"
        suite.set("tests", str(s_tests))
        suite.set("failures", str(s_fail))
        suite.set("errors", str(s_err))
        suite.set("skipped", str(s_skip))
        total, failures, errors, skipped = total + s_tests, failures + s_fail, errors + s_err, skipped + s_skip
    root.set("tests", str(total))
    root.set("failures", str(failures))
    root.set("errors", str(errors))
    root.set("skipped", str(skipped))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _detail(r: Result) -> str:
    lines = [r.message]
    if r.expected is not None:
        lines.append(f"expected: {r.expected}")
    if r.actual is not None:
        lines.append(f"actual: {r.actual}")
    if r.note:
        lines.append(f"note: {r.note}")
    if r.hint:
        lines.append(f"hint: {r.hint}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- profiles
#: Parameters that only tune how a recommendation is compared (they set no hard limit themselves).
_MODIFIERS = {"tolerance"}


def _recommendations_only(params: dict[str, Any]) -> bool:
    """True if a rule sets only ``recommended*`` values, so it can never fail above ``warn``."""
    keys = [k for k in params if k not in COMMON_PARAMS and k not in _MODIFIERS]
    return bool(keys) and all(k.startswith("recommended") for k in keys)


def _rule_rows(profile: Profile) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for cid, cdef in REGISTRY.items():
        if cid not in profile.rules:
            continue
        params = profile.rules[cid]
        severity = str(params.get("severity", cdef.severity))
        if severity == "error" and _recommendations_only(params):
            severity = "warn"  # a missed recommendation is never reported above warn (see run_checks)
        rows.append(
            (cid, describe_rule(cid, params), severity, str(params.get("basis") or ""), str(params.get("note") or ""))
        )
    return rows


def render_profile_text(profile: Profile) -> str:
    lines = [f"{profile.id}: {profile.title}"]
    if profile.extends:
        lines.append(f"  extends: {', '.join(profile.extends)}")
    if profile.origin != "builtin":
        lines.append(f"  file: {profile.origin}")
    if profile.description:
        lines.append(f"  {profile.description}")
    lines.append("")
    rows = _rule_rows(profile)
    w1 = max((len(r[0]) for r in rows), default=4)
    w3 = max((len(r[2]) for r in rows), default=5)
    for cid, req, sev, basis, note in rows:
        line = f"  {cid:<{w1}}  {sev:<{w3}}  {req}"
        if basis:
            line += f"  [{basis}]"
        lines.append(line)
        if note:
            lines.append(f"  {'':<{w1}}  {'':<{w3}}  note: {note}")
    if profile.notes:
        lines.append("")
        lines.extend(f"  * {n}" for n in profile.notes)
    if profile.sources:
        lines.append("")
        for src in profile.sources:
            lines.append(f"  source: {src.get('title', '')} <{src.get('url', '')}> (checked {src.get('checked', '?')})")
    return "\n".join(lines) + "\n"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_profile_markdown(profile: Profile) -> str:
    """Markdown rules table for a profile (used by `profiles show --format markdown` and the docs)."""
    lines = ["| Check | Requirement | Severity | Basis |", "| --- | --- | --- | --- |"]
    for cid, req, sev, basis, note in _rule_rows(profile):
        req_cell = _md_escape(req) + (f"<br><sub>{_md_escape(note)}</sub>" if note else "")
        lines.append(f"| `{cid}` | {req_cell} | {sev} | {basis or '-'} |")
    return "\n".join(lines) + "\n"


def write_text(path: str, text: str) -> None:
    """Write a report file (UTF-8), creating parent directories."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
