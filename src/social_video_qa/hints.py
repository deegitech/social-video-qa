"""One-line fixes for the errors people actually hit.

svqa calls no platform API, so its error codes are the messages of FFmpeg, FFprobe, the operating system and
svqa itself. :func:`hint_for` maps the known ones to the exact fix. Reports, ``svqa fix`` results and command
errors print it after the message (``hint: ...``), and the JSON output carries it as ``hint``.
docs/troubleshooting.md lists the same cases in a table, together with the platforms' own upload errors.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass

#: The extensions ``svqa check`` and ``svqa fix`` look for in folders (a test keeps this in step with the CLI).
VIDEO_EXTS_TEXT = ".mp4 .mov .m4v .webm .mkv .avi .ts .3gp"
#: What the setup guide installs from (pipx, uv and pip all accept it).
INSTALL_SPEC = "git+https://github.com/deegitech/social-video-qa"

_FULL_BUILD = (
    "install a full FFmpeg 4.4+ build (macOS: brew install ffmpeg; Debian/Ubuntu: sudo apt install ffmpeg) "
    "or point SVQA_FFMPEG at one"
)
_REINSTALL = "reinstall FFmpeg (macOS: brew reinstall ffmpeg; Debian/Ubuntu: sudo apt install --reinstall ffmpeg)"
_DOCTOR = "then run `svqa doctor`"


# ---------------------------------------------------------------------- how svqa was installed
def installer(prefix: str | None = None) -> str:
    """``pipx``, ``uv`` (a ``uv tool`` environment) or ``pip``: what manages the environment svqa runs in.

    pipx writes ``pipx_metadata.json`` and ``uv tool`` writes ``uv-receipt.toml`` into each environment they create.
    """
    root = sys.prefix if prefix is None else prefix
    if os.path.isfile(os.path.join(root, "pipx_metadata.json")):
        return "pipx"
    if os.path.isfile(os.path.join(root, "uv-receipt.toml")):
        return "uv"
    return "pip"


def _uv_python() -> str:
    """``--python 3.12``: keeps a ``uv tool`` reinstall on the Python svqa runs on now (old systems have older ones)."""
    return f"--python {sys.version_info.major}.{sys.version_info.minor}"


def add_package_command(package: str, prefix: str | None = None) -> str:
    """The command that adds the optional ``package`` (PyYAML, tomli) to svqa's own environment."""
    kind = installer(prefix)
    if kind == "pipx":
        return f"pipx inject social-video-qa {package}"
    if kind == "uv":
        return f"uv tool install {_uv_python()} --with {package} {INSTALL_SPEC}"
    return f"{shlex.quote(sys.executable)} -m pip install {package}"


def reinstall_command(prefix: str | None = None) -> str:
    """The command that reinstalls svqa the way it was installed."""
    kind = installer(prefix)
    if kind == "pipx":
        return f"pipx install --force {INSTALL_SPEC}"
    if kind == "uv":
        return f"uv tool install {_uv_python()} --reinstall {INSTALL_SPEC}"
    return f'{shlex.quote(sys.executable)} -m pip install --force-reinstall "social-video-qa @ {INSTALL_SPEC}"'


@dataclass(frozen=True)
class Hint:
    """A message pattern and its fix (a fixed text, or a function of the match)."""

    pattern: re.Pattern[str]
    fix: str | Callable[[re.Match[str]], str]

    def text(self, match: re.Match[str]) -> str:
        return self.fix(match) if callable(self.fix) else self.fix


def _encoder(m: re.Match[str]) -> str:
    name = m.group(1)
    why = " (minimal builds and Fedora's ffmpeg-free leave it out)" if name == "libx264" else ""
    return f"This FFmpeg build has no {name} encoder{why}: {_FULL_BUILD}, {_DOCTOR}."


def _rule(pattern: str, fix: str | Callable[[re.Match[str]], str]) -> Hint:
    return Hint(re.compile(pattern), fix)


#: Checked in order; the first match wins, so specific patterns come before general ones.
HINTS: tuple[Hint, ...] = (
    # ------------------------------------------------------------ the binaries
    _rule(
        r"\b(?:ffmpeg|ffprobe) not found\b",
        "Run `svqa doctor`: it shows which binary is missing and the install command for this system.",
    ),
    _rule(
        r"Library not loaded|error while loading shared libraries",
        f"FFmpeg cannot load its shared libraries (often after a partial upgrade): {_REINSTALL}, {_DOCTOR}.",
    ),
    _rule(
        r"Bad CPU type in executable",
        "The binary was built for another processor (for example an Intel-only FFmpeg on Apple silicon without "
        "Rosetta): install a native build (macOS: brew install ffmpeg) or point SVQA_FFMPEG / SVQA_FFPROBE at one, "
        f"{_DOCTOR}.",
    ),
    _rule(
        r"\b(?:ffmpeg|ffprobe) could not be started|\bnice: .*?(?:Permission denied|Exec format error)",
        f"The binary does not start: {_REINSTALL}, or fix SVQA_FFMPEG / SVQA_FFPROBE, {_DOCTOR}.",
    ),
    # ------------------------------------------------------------ what the FFmpeg build lacks
    _rule(r"Unknown encoder '([^']+)'", _encoder),
    _rule(
        r"No such filter: '([^']+)'",
        lambda m: f"This FFmpeg build has no '{m.group(1)}' filter: {_FULL_BUILD}, {_DOCTOR}.",
    ),
    _rule(
        r"Unknown input format: 'lavfi'",
        f"This FFmpeg build has no lavfi input, which `svqa fix` uses to add a silent audio track: {_FULL_BUILD}.",
    ),
    _rule(
        r"Unrecognized option '(?:show_format|show_streams|show_entries|select_streams|of)'",
        "The ffprobe in use is not ffprobe (is SVQA_FFPROBE pointing at ffmpeg?): point it at the ffprobe binary, "
        f"{_DOCTOR}.",
    ),
    _rule(
        r"for option 'nostdin'",
        "The ffmpeg in use is really ffprobe (is SVQA_FFMPEG pointing at ffprobe?): point it at the ffmpeg binary, "
        f"{_DOCTOR}.",
    ),
    _rule(
        r"Unrecognized option '([^']+)'",
        lambda m: f"This FFmpeg is too old for the option '{m.group(1)}': install FFmpeg 4.4 or newer, {_DOCTOR}.",
    ),
    _rule(r"Encoder not found", f"This FFmpeg build lacks an encoder svqa needs (libx264 or aac): {_FULL_BUILD}."),
    _rule(r"Filter not found", f"This FFmpeg build lacks a filter svqa needs: {_FULL_BUILD}, {_DOCTOR}."),
    _rule(
        r"loudnorm (?:pass 1 printed no measurements|measurement)",
        "Update FFmpeg to 4.4 or newer, or leave loudness alone with --no-loudnorm.",
    ),
    _rule(
        r"ffprobe returned invalid JSON",
        f"Point SVQA_FFPROBE at the ffprobe that belongs to your ffmpeg (same install), {_DOCTOR}.",
    ),
    # ------------------------------------------------------------ the input file
    _rule(
        r"moov atom not found",
        "The MP4/MOV index (moov box) is missing: the file is incomplete (an export, recording or copy was cut off) "
        "or is not really an MP4. Export or copy it again; `svqa fix` cannot repair it.",
    ),
    _rule(
        r"unsupported container",
        "If it is a video in another container, convert it first (export as MP4, or "
        "`ffmpeg -i INPUT -c copy OUTPUT.mp4`). Playlists and concat scripts are refused on purpose.",
    ),
    _rule(
        r"Invalid data found when processing input",
        "FFprobe cannot parse the file: it is damaged, not a video, or has the wrong extension. Check that it plays, "
        "then export it again.",
    ),
    _rule(r"ffprobe found no streams", "The file has no audio or video stream FFprobe can read: export it again."),
    _rule(r"file is empty", "The file has 0 bytes: the export or copy did not finish. Export it again."),
    _rule(r"not a regular file", "Give svqa video files or folders; devices, pipes and sockets are not read."),
    _rule(r"no video files found", f"svqa looks for {VIDEO_EXTS_TEXT} files; hidden files and folders are skipped."),
    # ------------------------------------------------------------ the file system
    _rule(r"No space left on device", "The disk is full: free some space, or write to another disk with -O DIR."),
    _rule(
        r"Permission denied",
        "Make sure you can read the input and write to the output folder (svqa fix: choose another with -O DIR).",
    ),
    _rule(r"No such file or directory", "Check the path, and quote names that contain spaces."),
    _rule(
        r"timed out after",
        "An analysis step hit --timeout (default 600 s): raise it (for example --timeout 1800), or use --fast to "
        "skip the decoding analyses.",
    ),
    # ------------------------------------------------------------ profiles
    _rule(
        r"unknown profile '",
        "`svqa profiles` lists them. Your own profiles need --profile-dir DIR or SVQA_PROFILE_PATH, or a path such "
        "as ./my-profile.json.",
    ),
    _rule(
        r"profile file not found",
        "Check the path: on the command line it is relative to the current folder, in `extends` to the folder of "
        "the profile that names it.",
    ),
    _rule(
        r"YAML profiles need PyYAML",
        lambda _m: f"Add PyYAML to svqa's environment: {add_package_command('PyYAML')}. Or write the profile in JSON.",
    ),
    _rule(
        r"TOML profiles need Python 3\.11",
        lambda _m: f"On Python 3.10, add tomli to svqa's environment: {add_package_command('tomli')}.",
    ),
    _rule(
        r"\) is invalid:",
        "`svqa profiles validate FILE` lists every problem; docs/writing-profiles.md lists the keys.",
    ),
    # ------------------------------------------------------------ svqa fix
    _rule(
        r"cannot be fixed automatically",
        "Fix it in your editor and export again. If it is intended, switch the check off (--skip CHECK, or in your "
        "own profile), or leave out --strict for warnings.",
    ),
    _rule(
        r"cannot fit the size/bitrate limits",
        "The video is too long for the size limit: shorten or split it (if it is over the maximum duration, --trim "
        "cuts it there).",
    ),
    _rule(
        r"still fails: ",
        "Re-run with --keep-failed --show-commands to inspect the output. If the profile's fix settings cannot "
        "reach the limit, open an issue with the `svqa probe` output.",
    ),
)


def hint_for(message: str | None) -> str | None:
    """The one-line fix for ``message``, or ``None`` if it is not a known error."""
    if not message:
        return None
    for hint in HINTS:
        match = hint.pattern.search(message)
        if match:
            return hint.text(match)
    return None
