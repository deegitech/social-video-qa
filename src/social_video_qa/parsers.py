"""Pure parsers for FFmpeg/FFprobe text output (kept separate so they are easy to test)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

_NUM = r"(-?inf|-?\d+(?:\.\d+)?)"


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("inf", "+inf"):
        return float("inf")
    if t == "-inf":
        return float("-inf")
    try:
        return float(t)
    except ValueError:
        return None


def parse_fraction(value: Any) -> float | None:
    """``"30000/1001" -> 29.97``; ``"0/0"``, ``"N/A"`` and junk give ``None``."""
    if value is None:
        return None
    try:
        frac = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return float(frac) if frac > 0 else None


@dataclass
class Ebur128Summary:
    integrated_lufs: float | None
    threshold_lufs: float | None
    lra_lu: float | None
    true_peak_dbtp: float | None


def parse_ebur128(stderr: str) -> Ebur128Summary | None:
    """Parse the final ``Summary:`` block printed by FFmpeg's ``ebur128`` filter."""
    idx = stderr.rfind("Summary:")
    if idx < 0:
        return None
    block = stderr[idx:]
    integrated = re.search(r"Integrated loudness:\s*I:\s*" + _NUM + r"\s*LUFS", block)
    threshold = re.search(r"Integrated loudness:\s*I:[^\n]*\n\s*Threshold:\s*" + _NUM, block)
    lra = re.search(r"LRA:\s*" + _NUM + r"\s*LU\b", block)
    peak = re.search(r"True peak:\s*Peak:\s*" + _NUM, block)
    if not integrated:
        return None
    return Ebur128Summary(
        integrated_lufs=_num(integrated.group(1)),
        threshold_lufs=_num(threshold.group(1)) if threshold else None,
        lra_lu=_num(lra.group(1)) if lra else None,
        true_peak_dbtp=_num(peak.group(1)) if peak else None,
    )


def parse_loudnorm_json(stderr: str) -> dict[str, str] | None:
    """Extract the JSON object printed by ``loudnorm=print_format=json``."""
    end = stderr.rfind("}")
    start = stderr.rfind("{", 0, end + 1)
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "input_i" not in data:
        return None
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class Segment:
    start: float
    end: float | None
    duration: float | None


def parse_blackdetect(stderr: str) -> list[Segment]:
    """Black segments reported by ``blackdetect``."""
    out = []
    for m in re.finditer(r"black_start:\s*(\S+)\s+black_end:\s*(\S+)\s+black_duration:\s*(\S+)", stderr):
        start, end, dur = (_num(g) for g in m.groups())
        if start is not None:
            out.append(Segment(start, end, dur))
    return out


def parse_freezedetect(stderr: str) -> list[Segment]:
    """Frozen segments reported by ``freezedetect``. ``end`` is ``None`` if still frozen at EOF."""
    segs: list[Segment] = []
    for m in re.finditer(r"freeze_(start|duration|end):\s*(\S+)", stderr):
        key, val = m.group(1), _num(m.group(2))
        if val is None:
            continue
        if key == "start":
            segs.append(Segment(val, None, None))
        elif segs and key == "duration":
            segs[-1].duration = val
        elif segs and key == "end":
            segs[-1].end = val
    return segs


def parse_encoder_settings(head: bytes) -> dict[str, Any] | None:
    """Find the x264/x265 settings string encoders embed in the bitstream (SEI).

    Returns e.g. ``{"encoder": "x264", "bframes": 0, "open_gop": 0, "keyint": 60}``.
    """
    m = re.search(rb"x264 - core \d+[^\x00]{0,4000}", head)
    if m:
        s = m.group(0).decode("latin-1")
        out: dict[str, Any] = {"encoder": "x264"}
        for key in ("bframes", "open_gop", "keyint", "b_pyramid"):
            mm = re.search(rf"\b{key}=(\d+|infinite)", s)
            if mm:
                out[key] = None if mm.group(1) == "infinite" else int(mm.group(1))
        mm = re.search(r"\brc=(\w+)", s)
        if mm:
            out["rc"] = mm.group(1)
        mm = re.search(r"\bnal_hrd=(\w+)", s)
        if mm:
            out["nal_hrd"] = mm.group(1)
        return out
    m = re.search(rb"x265 \(build \d+\)[^\x00]{0,6000}", head)
    if m:
        s = m.group(0).decode("latin-1")
        out = {"encoder": "x265"}
        mm = re.search(r"\bbframes=(\d+)", s)
        if mm:
            out["bframes"] = int(mm.group(1))
        if re.search(r"\bno-open-gop\b", s):
            out["open_gop"] = 0
        elif re.search(r"\bopen-gop\b", s):
            out["open_gop"] = 1
        mm = re.search(r"\bkeyint=(\d+)", s)
        if mm:
            out["keyint"] = int(mm.group(1))
        return out
    return None


@dataclass
class Packet:
    pts: int | None
    dts: int | None
    duration: int | None
    size: int
    key: bool


def parse_packets(text: str) -> list[Packet]:
    """Parse ``ffprobe -show_entries packet=pts,dts,duration,size,flags -of compact=p=0``."""
    out: list[Packet] = []
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        fields: dict[str, str] = {}
        for part in line.split("|"):
            k, sep, v = part.partition("=")
            if sep:
                fields[k.strip()] = v.strip()

        def num(key: str, fields: dict[str, str] = fields) -> int | None:
            v = fields.get(key)
            if v is None or v in ("N/A", ""):
                return None
            try:
                return int(v)
            except ValueError:
                return None

        out.append(
            Packet(
                pts=num("pts"),
                dts=num("dts"),
                duration=num("duration"),
                size=num("size") or 0,
                key="K" in fields.get("flags", ""),
            )
        )
    return out
