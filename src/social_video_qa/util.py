"""Small helpers: hashing, atomic writes, timestamps and number formatting."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

MB = 1_000_000  # platforms quote decimal megabytes; decimal is also the stricter reading


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_json(obj: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable object (sorted keys, compact)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def utc_now() -> str:
    """Current UTC time as an ISO 8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: str, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically: temp file in the same directory, fsync, rename.

    The file is created with mode 0600; pass ``mode`` to give the result other permissions.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".svqa-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_ratio(value: Any) -> float | None:
    """Parse ``"9:16"``, ``"16/9"``, ``0.5625`` or ``"0.5625"`` into a float width/height ratio."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    for sep in (":", "/"):
        if sep in text:
            a, _, b = text.partition(sep)
            try:
                num, den = float(a), float(b)
            except ValueError:
                return None
            return num / den if num > 0 and den > 0 else None
    try:
        num = float(text)
    except ValueError:
        return None
    return num if num > 0 else None


def parse_size(value: Any) -> tuple[int, int] | None:
    """Parse ``"1080x1920"`` into ``(1080, 1920)``."""
    if not isinstance(value, str):
        return None
    a, sep, b = value.lower().replace(" ", "").partition("x")
    if not sep or not a.isdigit() or not b.isdigit():
        return None
    w, h = int(a), int(b)
    return (w, h) if w > 0 and h > 0 else None


def ratio_label(ratio: float | None) -> str:
    """Human label for a width/height ratio, e.g. ``0.5625 -> "9:16"``."""
    if not ratio:
        return "unknown"
    inverse = 1 / ratio
    if ratio < 1 and inverse >= 32 and abs(inverse - round(inverse)) / inverse < 0.002:
        return f"1:{round(inverse)}"  # extreme portrait limits such as 0.01 -> 1:100
    frac = Fraction(ratio).limit_denominator(32)
    if abs(float(frac) - ratio) / ratio < 0.002:
        return f"{frac.numerator}:{frac.denominator}"
    return f"{ratio:.4f}:1"


def fmt_num(value: float | None, digits: int = 2) -> str:
    """Format a number compactly (no trailing zeros); ``None`` becomes ``"?"``."""
    if value is None:
        return "?"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def fmt_bytes(n: int | None) -> str:
    """Format a byte count in decimal units (MB = 1,000,000 bytes)."""
    if n is None:
        return "?"
    if n >= 1000 * MB:
        return f"{n / (1000 * MB):.2f} GB"
    if n >= MB:
        return f"{n / MB:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} kB"
    return f"{n} B"


def fmt_kbps(kbps: float | None) -> str:
    """Format a bitrate given in kbps, switching to Mbps above 1000 kbps."""
    if kbps is None:
        return "?"
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps:.0f} kbps"


def round_floats(obj: Any, digits: int = 3) -> Any:
    """Recursively round floats for stable, readable JSON output."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):  # NaN / inf are not valid JSON
            return None
        return round(obj, digits)
    if isinstance(obj, dict):
        return {k: round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, digits) for v in obj]
    return obj
