"""Provisional Python API (may change before 1.0)."""

from __future__ import annotations

from collections.abc import Iterable

from .checks import CheckReport, needs_for, run_checks, window_for
from .ffmpeg import Runner
from .probe import NEEDS_ALL, MediaInfo, analyze
from .profiles import Profile
from .profiles import load_profile as _load_profile


def load_profile(ref: str, extra_dirs: Iterable[str] = ()) -> Profile:
    """Load a built-in or user profile by ID or path (``extends`` resolved, validated)."""
    return _load_profile(ref, extra_dirs)


def analyze_file(path: str, *, runner: Runner | None = None, fast: bool = False) -> MediaInfo:
    """Measure a file. ``fast`` skips packet timing, loudness, hiss and start-content analysis."""
    runner = runner or Runner.discover()
    return analyze(path, runner, frozenset() if fast else NEEDS_ALL)


def check_file(
    path: str,
    profile: Profile | str,
    *,
    runner: Runner | None = None,
    skip: Iterable[str] = (),
    only: Iterable[str] = (),
    strict: bool = False,
    fast: bool = False,
) -> CheckReport:
    """Check one file against one profile."""
    prof = load_profile(profile) if isinstance(profile, str) else profile
    runner = runner or Runner.discover()
    needs = set() if fast else needs_for(prof.rules, skip, only)
    media = analyze(path, runner, needs, start_window_s=window_for(prof.rules))
    return run_checks(
        media, prof.rules, file=path, profile=prof.id, profile_title=prof.title, skip=skip, only=only, strict=strict
    )
