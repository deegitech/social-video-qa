"""Locate and run FFmpeg/FFprobe safely.

Every call goes through :class:`Runner`, which

* passes arguments as a list (never through a shell) and closes stdin;
* opens user files only as ``file:`` URLs with ``-protocol_whitelist file`` and a
  demuxer whitelist, so a crafted playlist (HLS), ``ffconcat`` script or similar
  cannot make FFmpeg open network URLs or other local files;
* applies a timeout and, on POSIX, runs FFmpeg under ``nice`` so checks and fixes
  stay polite on a shared machine or CI runner.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Demuxers this tool will open. ``mov`` covers MP4/MOV/M4V/3GP, ``matroska`` covers
#: MKV/WebM. Anything else (HLS playlists, concat scripts, image sequences, devices…)
#: is refused by FFmpeg itself.
DEMUXER_WHITELIST = "mov,matroska,avi,mpegts"

_UNSET: Any = object()


class FFmpegError(RuntimeError):
    """FFmpeg or FFprobe could not be run or reported a failure."""


class ToolNotFound(FFmpegError):
    """``ffmpeg`` or ``ffprobe`` is not installed (or not on ``PATH``)."""


def hardened_input(path: str) -> list[str]:
    """Input arguments that restrict FFmpeg to a plain local file in a known container."""
    return [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        DEMUXER_WHITELIST,
        "-i",
        "file:" + path,
    ]


def _which(name: str, env_var: str, env: Mapping[str, str] | None = None) -> str | None:
    override = (os.environ if env is None else env).get(env_var, "").strip()
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return shutil.which(override)
    return shutil.which(name)


@dataclass(frozen=True)
class Location:
    """Where a binary was found: ``origin`` is the environment variable that named it, or ``PATH``."""

    name: str
    path: str | None
    origin: str
    override: str = ""  # the environment variable's value, when it is set


def locate(name: str, env_var: str, env: Mapping[str, str] | None = None) -> Location:
    """Find ``name`` the way :meth:`Runner.discover` does (``env_var`` first, then ``PATH``)."""
    override = (os.environ if env is None else env).get(env_var, "").strip()
    return Location(name, _which(name, env_var, env), env_var if override else "PATH", override)


def tail(text: str, limit: int = 800) -> str:
    """Last ``limit`` characters of ``text`` (FFmpeg puts the useful error at the end)."""
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


#: The "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x7f8e...] " prefix of FFmpeg log lines.
_LOG_PREFIX = re.compile(r"^\[[^\]]*@ (?:0x)?[0-9a-fA-F]+\]\s*")
#: Lines that never say why: FFmpeg 7+ repeats ("Error opening output file -.", "Error selecting an encoder")
#: and the version banner a program prints when it stops before reading -hide_banner.
_NOISE = re.compile(
    r"^(?:Error opening (?:input|output) file .*\.$|Error selecting an encoder$"
    r"|ff(?:mpeg|probe|play) version |built with |configuration: "
    r"|lib(?:avutil|avcodec|avformat|avdevice|avfilter|swscale|swresample|postproc)\s)"
)
#: Lines that name the cause. They are kept even when other lines follow them.
_CAUSE = re.compile(
    r"No such filter: |Unknown encoder |Unknown input format: |Unrecognized option |moov atom not found"
    r"|No space left on device|Library not loaded|error while loading shared libraries|Bad CPU type"
)


def ffmpeg_reason(stderr: str, path: str | None = None, lines: int = 2) -> str:
    """The last ``lines`` distinct lines of an FFmpeg log that say something, without ``[demuxer @ 0x…]`` prefixes.

    FFmpeg often prints the cause one line before the summary ("moov atom not found", then "Invalid data
    found when processing input"), so one line alone hides it. FFmpeg 7 and newer add lines that only repeat
    that something failed ("Error opening output file -."); they are skipped, and a line that names the cause
    ("No such filter: 'ebur128'") is kept even when more lines follow it. ``file:<path>: `` prefixes are
    dropped, because reports already name the file.
    """
    useful: list[str] = []
    for raw in (stderr or "").strip().splitlines():
        text = _LOG_PREFIX.sub("", raw.strip())
        if path:
            text = text.replace(f"file:{path}: ", "")
        if text and not _NOISE.match(text):
            useful.append(text)
    picked: list[str] = []
    for text in reversed(useful):
        if text not in picked:
            picked.insert(0, text)
        if len(picked) >= lines:
            break
    cause = next((text for text in reversed(useful) if _CAUSE.search(text)), None)
    if cause is not None and cause not in picked:
        picked = [cause, *picked[1:]]
    return "; ".join(picked)


#: The first line of ``-version`` output names the program: "ffprobe version 8.1.2 Copyright ...".
_PROGRAM_RE = re.compile(r"^\s*(ffmpeg|ffprobe|ffplay)\s+version\b")


def program_name(version_output: str) -> str | None:
    """``ffmpeg``, ``ffprobe`` or ``ffplay``: the program that printed this ``-version`` output, if it says."""
    lines = (version_output or "").strip().splitlines()
    match = _PROGRAM_RE.match(lines[0]) if lines else None
    return match.group(1) if match else None


def listed_names(output: str) -> set[str]:
    """Names in ``ffmpeg -encoders / -filters / -devices`` and ``ffprobe -demuxers`` listings.

    Each entry is a flags column and then a name, or comma-separated names (``mov,mp4,m4a,3gp,3g2,mj2``).
    Newer FFmpeg marks devices in the demuxer list with a separate ``d`` column, which is skipped.
    """
    names: set[str] = set()
    for line in (output or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] == "=":
            continue
        token = parts[2] if parts[1] == "d" and len(parts) > 2 else parts[1]
        names.update(n for n in token.split(",") if n)
    return names


_VERSION_RE = re.compile(r"version\s+n?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(first_line: str) -> tuple[str | None, tuple[int, int] | None]:
    """Parse ``ffmpeg -version`` output. Returns ``(label, (major, minor))``.

    Git builds (``N-12345-g…``) have no numeric version; they return ``(label, None)``
    and are treated as recent.
    """
    line = (first_line or "").strip().splitlines()[0] if first_line and first_line.strip() else ""
    m = re.search(r"version\s+(\S+)", line)
    label = m.group(1) if m else None
    nm = _VERSION_RE.search(line)
    if nm:
        return label, (int(nm.group(1)), int(nm.group(2)))
    return label, None


@dataclass
class Runner:
    """Runs ``ffmpeg``/``ffprobe`` with the safety and resource settings above."""

    ffmpeg: str
    ffprobe: str
    threads: int = 2
    nice: int = 10
    timeout: float | None = 600.0
    _version: tuple[str | None, tuple[int, int] | None] | None = field(default=None, repr=False)
    _listings: dict[str, set[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def discover(cls, *, threads: int = 2, nice: int = 10, timeout: float | None = 600.0) -> Runner:
        """Find the binaries on ``PATH`` (or via ``SVQA_FFMPEG`` / ``SVQA_FFPROBE``)."""
        ffmpeg = _which("ffmpeg", "SVQA_FFMPEG")
        ffprobe = _which("ffprobe", "SVQA_FFPROBE")
        missing = [name for name, found in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not found]
        if missing:
            raise ToolNotFound(
                f"{' and '.join(missing)} not found. Install FFmpeg (https://ffmpeg.org/download.html) "
                "or point SVQA_FFMPEG / SVQA_FFPROBE at the binaries."
            )
        assert ffmpeg and ffprobe  # for type checkers
        return cls(ffmpeg=ffmpeg, ffprobe=ffprobe, threads=max(1, threads), nice=nice, timeout=timeout)

    # ------------------------------------------------------------------ process
    def _argv(self, args: list[str]) -> list[str]:
        if self.nice and os.name == "posix":
            nice_bin = shutil.which("nice")
            if nice_bin:
                return [nice_bin, "-n", str(self.nice), *args]
        return list(args)

    def run(
        self,
        args: list[str],
        *,
        timeout: float | None = _UNSET,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``args`` (``args[0]`` is the ffmpeg/ffprobe path) and capture text output."""
        limit = self.timeout if timeout is _UNSET else timeout
        env = dict(os.environ, AV_LOG_FORCE_NOCOLOR="1")
        name = os.path.basename(args[0])
        try:
            proc = subprocess.run(
                self._argv(args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(f"{name} timed out after {limit:g} s") from exc
        except OSError as exc:  # removed, not executable, or built for another processor
            raise ToolNotFound(f"{name} could not be started: {exc}") from exc
        if check and proc.returncode != 0:
            # One line, so reports keep their layout; the cause line is kept (see ffmpeg_reason).
            reason = ffmpeg_reason(proc.stderr, lines=3) or "no error output"
            raise FFmpegError(f"{name} exited with code {proc.returncode}: {reason}")
        return proc

    # ------------------------------------------------------------------ info
    def version(self) -> tuple[str | None, tuple[int, int] | None]:
        """``(label, (major, minor))`` of the ffmpeg binary, cached."""
        if self._version is None:
            try:
                out = self.run([self.ffmpeg, "-hide_banner", "-version"], timeout=30).stdout
            except FFmpegError:
                out = ""
            self._version = parse_version(out)
        return self._version

    def version_label(self) -> str | None:
        return self.version()[0]

    def _listing(self, binary: str, flag: str) -> set[str]:
        key = f"{binary} {flag}"
        if key not in self._listings:
            try:
                out = self.run([binary, "-hide_banner", flag], timeout=30, check=False).stdout
            except FFmpegError:
                out = ""
            self._listings[key] = listed_names(out)
        return self._listings[key]

    def encoder_names(self) -> set[str]:
        """Encoders this ffmpeg build has (``ffmpeg -encoders``), cached."""
        return self._listing(self.ffmpeg, "-encoders")

    def filter_names(self) -> set[str]:
        """Filters this ffmpeg build has (``ffmpeg -filters``), cached."""
        return self._listing(self.ffmpeg, "-filters")

    def device_names(self) -> set[str]:
        """Input/output devices this ffmpeg build has (``ffmpeg -devices``), cached."""
        return self._listing(self.ffmpeg, "-devices")

    def demuxer_names(self) -> set[str]:
        """Demuxers this ffprobe build has (``ffprobe -demuxers``), cached."""
        return self._listing(self.ffprobe, "-demuxers")

    def has_encoder(self, name: str) -> bool:
        return name in self.encoder_names()

    def has_filter(self, name: str) -> bool:
        return name in self.filter_names()
