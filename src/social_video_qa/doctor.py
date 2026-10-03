"""``svqa doctor``: read-only checks of everything ``svqa check`` and ``svqa fix`` rely on.

Each item is reported as ready (✓), a problem (✗, always followed by the exact fix), worth a look (!) or optional
and unused (-). The command exits 0 when nothing is a problem and 1 otherwise.

Nothing is written and nothing goes over the network. The last item pushes half a second of synthetic video and
audio (FFmpeg's own test sources) through libx264, AAC, loudnorm and ebur128 into FFmpeg's null muxer, which
proves the whole chain works, not just that the parts are listed. svqa uses no credentials, so there are none to
check, and doctor prints only paths and versions.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import __version__
from .ffmpeg import FFmpegError, Location, Runner, ffmpeg_reason, locate, parse_version, program_name
from .hints import add_package_command, hint_for, reinstall_command
from .profiles import EXTENSIONS, PROFILE_PATH_ENV, ProfileError, builtin_ids, load_builtin, load_profile
from .report import Style

MIN_FFMPEG = (4, 4)
DOCS_URL = "https://github.com/deegitech/social-video-qa/blob/main/docs/"
#: The variable that overrides each binary.
VARS = {"ffmpeg": "SVQA_FFMPEG", "ffprobe": "SVQA_FFPROBE"}

#: Filters the analyses of ``svqa check`` run, and what they measure.
CHECK_FILTERS: dict[str, str] = {
    "ebur128": "loudness, true peak, silence and hiss",
    "highpass": "the hiss index",
    "blackdetect": "black openings",
    "freezedetect": "frozen openings",
}
#: Filters ``svqa fix`` builds its filter graphs from.
FIX_FILTERS: tuple[str, ...] = (
    "loudnorm",
    "aresample",
    "afade",
    "anullsrc",
    "scale",
    "pad",
    "crop",
    "split",
    "boxblur",
    "overlay",
    "fps",
    "format",
    "setsar",
    "bwdif",
)
#: Encoders ``svqa fix`` writes with.
ENCODERS: dict[str, str] = {"libx264": "needed to re-encode video", "aac": "needed to re-encode audio"}
#: Demuxers behind the input whitelist (see ffmpeg.DEMUXER_WHITELIST).
DEMUXERS: dict[str, str] = {"mov": "MP4/MOV", "matroska": "MKV/WebM", "avi": "AVI", "mpegts": "MPEG-TS"}
#: What the pipeline test needs, so it runs only when the items before it passed.
PIPELINE_NEEDS = ("loudnorm", "ebur128", "aresample", "fps", "format", "setsar")


@dataclass
class Item:
    """One line of the report."""

    name: str
    status: str  # ok | fail | warn | skip
    detail: str
    fix: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def install_lines(platform: str) -> list[str]:
    """How to install FFmpeg 4.4+ with libx264 on this kind of system."""
    if platform == "darwin":
        return ["brew install ffmpeg   (Homebrew: https://brew.sh)"]
    if platform.startswith("linux"):
        return [
            "Debian/Ubuntu: sudo apt install ffmpeg   (Ubuntu 20.04 and Debian 11 ship versions older than 4.4)",
            "Fedora: the ffmpeg package from RPM Fusion (Fedora's own ffmpeg-free has no libx264)",
            "any system: a static build linked from https://ffmpeg.org/download.html",
        ]
    return ["a build linked from https://ffmpeg.org/download.html"]


def upgrade_lines(platform: str) -> list[str]:
    """How to replace an FFmpeg older than 4.4 (``brew install`` only says "already installed" for an old brew one)."""
    if platform == "darwin":
        return ["brew upgrade ffmpeg   (or brew install ffmpeg if it did not come from Homebrew: https://brew.sh)"]
    return install_lines(platform)


def _reinstall(platform: str) -> str:
    if platform == "darwin":
        return "brew reinstall ffmpeg"
    if platform.startswith("linux"):
        return "Debian/Ubuntu: sudo apt install --reinstall ffmpeg"
    return "a fresh build from https://ffmpeg.org/download.html"


#: Added under install commands: a new FFmpeg does not help while an older one comes first on PATH.
USE_IT = "if another ffmpeg comes first on PATH, point SVQA_FFMPEG and SVQA_FFPROBE at the new one"


# ---------------------------------------------------------------------- items
def _python_item() -> Item:
    v = sys.version_info
    return Item("python", "ok", f"{v.major}.{v.minor}.{v.micro} (3.10 or newer needed)")


def _tool_item(loc: Location, platform: str) -> Item:
    var = VARS[loc.name]
    if loc.path:
        return Item(loc.name, "ok", f"{loc.path} (from {loc.origin})")
    if loc.override:
        return Item(
            loc.name,
            "fail",
            f"{var} is set to {loc.override}, which is not an executable file",
            [f"point it at the {loc.name} binary: export {var}=/path/to/{loc.name}", f"or use PATH: unset {var}"],
        )
    return Item(
        loc.name,
        "fail",
        "not found on PATH",
        [*install_lines(platform), f"or point {var} at the binary: export {var}=/path/to/{loc.name}"],
    )


@dataclass
class Version:
    """What ``<binary> -version`` said."""

    label: str | None = None  # "8.1.2", "N-121000-g0123abcd"
    numbers: tuple[int, int] | None = None
    error: str | None = None  # why it did not run
    program: str | None = None  # the program the first line names: ffmpeg, ffprobe or ffplay


def _version(runner: Runner, binary: str) -> Version:
    """Run ``<binary> -version`` and read the program name and version from it."""
    try:
        proc = runner.run([binary, "-hide_banner", "-version"], timeout=30, check=False)
    except FFmpegError as exc:
        return Version(error=str(exc))
    if proc.returncode != 0:
        return Version(error=ffmpeg_reason(proc.stderr) or f"exit code {proc.returncode}")
    label, numbers = parse_version(proc.stdout)
    return Version(label, numbers, None, program_name(proc.stdout))


def _version_item(name: str, v: Version, platform: str) -> Item:
    item = f"{name} version"
    if v.error:
        retry = hint_for(v.error) or f"reinstall FFmpeg ({_reinstall(platform)}), then run svqa doctor again"
        return Item(item, "fail", f"{name} does not run: {v.error}", [retry])
    if v.label is None:
        return Item(item, "warn", "no version in its output", [f"check that it is FFmpeg's {name}: {name} -version"])
    if v.numbers is None:
        return Item(item, "ok", f"{v.label} (a git build: treated as recent)")
    if v.numbers < MIN_FFMPEG:
        upgrade = ["install a newer FFmpeg:", *upgrade_lines(platform), USE_IT]
        return Item(item, "fail", f"{v.label} is older than 4.4", upgrade)
    return Item(item, "ok", f"{v.label} (4.4 or newer needed)")


def _sibling(path: str, name: str) -> str:
    """``name`` in the folder of ``path`` (``/opt/homebrew/bin/ffprobe`` next to ``ffmpeg``), if it can run there."""
    ext = ".exe" if path.lower().endswith(".exe") else ""
    candidate = os.path.join(os.path.dirname(path), name + ext)
    if not _same_path(candidate, path) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return f"/path/to/{name}"


def _wrong_program_item(name: str, loc: Location, program: str, platform: str) -> Item:
    """``SVQA_FFPROBE=/usr/bin/ffmpeg`` and the like: the binary runs, but it is another FFmpeg program."""
    var = VARS[name]
    path = loc.path or ""
    export = f"export {var}={shlex.quote(_sibling(path, name))}"
    if loc.origin == var:
        return Item(f"{name} version", "fail", f"{var} points at {program} ({path}), not {name}", [export])
    return Item(
        f"{name} version",
        "fail",
        f"the {name} on PATH ({path}) is really {program}",
        [f"point {var} at the real {name}: {export}", f"or reinstall FFmpeg ({_reinstall(platform)})"],
    )


def _same_path(a: str, b: str) -> bool:
    """The same path, without resolving links (version-manager shims are links to one program by design)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _missing_item(name: str, wanted: Sequence[str], have: set[str], what: str, platform: str) -> Item:
    missing = [w for w in wanted if w not in have]
    if not missing:
        return Item(name, "ok", ", ".join(wanted))
    detail = f"missing: {', '.join(missing)} ({what})"
    return Item(name, "fail", detail, ["install a full FFmpeg build:", *install_lines(platform), USE_IT])


def _pipeline_item(runner: Runner, platform: str) -> Item:
    args = [runner.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", str(runner.threads)]
    args += ["-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30:duration=0.5"]
    args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5"]
    args += ["-map", "0:v", "-map", "1:a", "-vf", "fps=30,format=yuv420p,setsar=1"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-bf", "0", "-g", "30", "-flags", "+cgop"]
    args += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11,ebur128=peak=true,aresample=48000"]
    args += ["-c:a", "aac", "-b:a", "128k", "-f", "null", "-"]
    try:
        proc = runner.run(args, timeout=60, check=False)
        reason = None if proc.returncode == 0 else ffmpeg_reason(proc.stderr) or f"exit code {proc.returncode}"
    except FFmpegError as exc:
        reason = str(exc)
    if reason is None:
        return Item("pipeline test", "ok", "0.5 s of test video and audio through libx264, AAC, loudnorm, ebur128")
    fix = hint_for(reason) or (
        f"update or reinstall FFmpeg ({_reinstall(platform)}), run svqa doctor again, and open an issue with this "
        "output if it still fails"
    )
    return Item("pipeline test", "fail", f"a 0.5 s test encode failed: {reason}", [fix])


def _builtin_item() -> Item:
    ids = builtin_ids()
    bad: list[str] = []
    for pid in ids:
        try:
            load_builtin(pid)
        except (ValueError, OSError):  # ProfileError, or a damaged file in the installed package
            bad.append(pid)
    if not ids or bad:
        what = f"invalid: {', '.join(bad)}" if bad else "none found"
        return Item("built-in profiles", "fail", what, [f"reinstall svqa: {reinstall_command()}"])
    return Item("built-in profiles", "ok", f"{len(ids)} load and validate")


def _folder_items(profile_dirs: Sequence[str]) -> list[Item]:
    given = [d for d in profile_dirs if d]
    from_env = [d for d in os.environ.get(PROFILE_PATH_ENV, "").split(os.pathsep) if d.strip()]
    dirs = given + [d for d in from_env if d not in given]
    if not dirs:
        return [Item("profile folders", "skip", f"none set (optional: --profile-dir DIR or {PROFILE_PATH_ENV})")]
    items: list[Item] = []
    for folder in dirs:
        source = "--profile-dir" if folder in given else PROFILE_PATH_ENV
        if not os.path.isdir(folder):
            create = f"create it (mkdir -p {folder}) or correct {source}"
            items.append(Item("profile folder", "fail", f"{folder} does not exist (from {source})", [create]))
            continue
        try:
            entries = os.listdir(folder)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            access = f"make it readable (check with ls -ld {folder}) or correct {source}"
            items.append(Item("profile folder", "fail", f"{folder}: cannot list it ({reason})", [access]))
            continue
        names = sorted(n for n in entries if os.path.splitext(n)[1].lower() in EXTENSIONS and not n.startswith("."))
        bad: list[tuple[str, str]] = []
        for name in names:
            try:
                load_profile(os.path.join(folder, name), dirs)
            except ProfileError as exc:
                bad.append((name, str(exc)))
        if bad:
            first_name, first_error = bad[0]
            lines = first_error.splitlines()
            problems = [line.strip().lstrip("- ") for line in lines[1:]] if lines[0].endswith("is invalid:") else []
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            fix = [f"{first_name}: {problems[0] if problems else lines[0]}{more}"]
            hint = hint_for(first_error)
            if hint and "profiles validate" not in hint:
                fix.append(hint)
            # every invalid file, searched the way doctor searched (SVQA_PROFILE_PATH is read again by itself)
            validate = ["svqa", "profiles", "validate", *(os.path.join(folder, n) for n, _e in bad)]
            for directory in given:
                validate += ["--profile-dir", directory]
            fix.append(f"see every problem: {shlex.join(validate)}")
            listed = ", ".join(n for n, _e in bad[:3]) + (", ..." if len(bad) > 3 else "")
            items.append(
                Item("profile folder", "fail", f"{folder}: {len(bad)} of {len(names)} invalid ({listed})", fix)
            )
        else:
            items.append(Item("profile folder", "ok", f"{folder}: {len(names)} profile(s), all valid"))
    return items


def _formats_item() -> Item:
    yaml_ok = importlib.util.find_spec("yaml") is not None
    toml_ok = sys.version_info >= (3, 11) or importlib.util.find_spec("tomli") is not None
    have = ["JSON"] + (["YAML"] if yaml_ok else []) + (["TOML"] if toml_ok else [])
    extra = []
    if not yaml_ok:
        extra.append(f"YAML needs PyYAML: {add_package_command('PyYAML')}")
    if not toml_ok:
        extra.append(f"TOML needs tomli on Python 3.10: {add_package_command('tomli')}")
    if extra:
        return Item("profile formats", "skip", f"{', '.join(have)}; optional: " + "; ".join(extra))
    return Item("profile formats", "ok", ", ".join(have))


# ---------------------------------------------------------------------- public API
def diagnose(
    profile_dirs: Sequence[str] = (),
    *,
    platform: str | None = None,
    make_runner: Callable[[str, str], Runner] | None = None,
    threads: int = 2,
    nice: int = 10,
) -> list[Item]:
    """Run every read-only check, in order. ``make_runner`` lets tests replace FFmpeg."""
    platform = platform or sys.platform
    items = [_python_item()]
    ffmpeg, ffprobe = locate("ffmpeg", "SVQA_FFMPEG"), locate("ffprobe", "SVQA_FFPROBE")
    items += [_tool_item(ffmpeg, platform), _tool_item(ffprobe, platform)]

    working = False
    if ffmpeg.path and ffprobe.path:
        if make_runner is not None:
            runner = make_runner(ffmpeg.path, ffprobe.path)
        else:
            runner = Runner(ffmpeg=ffmpeg.path, ffprobe=ffprobe.path, threads=max(1, threads), nice=nice, timeout=60)
        ff, probe = _version(runner, runner.ffmpeg), _version(runner, runner.ffprobe)
        ff_item, probe_item = _version_item("ffmpeg", ff, platform), _version_item("ffprobe", probe, platform)
        # The first line of -version names the program, so a variable that points at the wrong one is caught
        # here instead of as "Unrecognized option" in every later run.
        wrong = False
        if ff.program and ff.program != "ffmpeg":
            ff_item, wrong = _wrong_program_item("ffmpeg", ffmpeg, ff.program, platform), True
        if probe.program and probe.program != "ffprobe":
            probe_item, wrong = _wrong_program_item("ffprobe", ffprobe, probe.program, platform), True
        if not wrong and _same_path(ffmpeg.path, ffprobe.path):
            wrong = True
            probe_item = Item(
                "ffprobe version",
                "fail",
                f"ffmpeg and ffprobe are the same file ({ffprobe.path})",
                ["point SVQA_FFMPEG and SVQA_FFPROBE at the ffmpeg and ffprobe binaries of one install"],
            )
        elif not wrong and probe_item.status == "ok" and ff.label and probe.label != ff.label:
            probe_item = Item(
                "ffprobe version",
                "warn",
                f"{probe.label}, but ffmpeg is {ff.label}: two different installs",
                ["use one install for both: point SVQA_FFMPEG and SVQA_FFPROBE at binaries in the same folder"],
            )
        elif not wrong and probe_item.status == "ok" and probe.label == ff.label:
            probe_item.detail = f"{probe.label} (same build as ffmpeg)"
        items += [ff_item, probe_item]
        working = not ff.error and not probe.error and not wrong

    if working:
        before = len(items)
        demuxers = runner.demuxer_names()
        cannot_open = ", ".join(DEMUXERS[d] for d in DEMUXERS if d not in demuxers)
        items.append(_missing_item("demuxers", list(DEMUXERS), demuxers, f"svqa cannot open {cannot_open}", platform))
        filters = runner.filter_names()
        check_item = _missing_item(
            "check filters",
            list(CHECK_FILTERS),
            filters,
            "svqa check cannot measure " + ", ".join(CHECK_FILTERS[f] for f in CHECK_FILTERS if f not in filters),
            platform,
        )
        if check_item.failed:
            check_item.fix.append("until then, svqa check --fast skips the analyses that need them")
        items.append(check_item)
        fix_item = _missing_item("fix filters", FIX_FILTERS, filters, "svqa fix needs them", platform)
        if "loudnorm" not in filters:
            fix_item.fix.append("until then, svqa fix --no-loudnorm leaves loudness alone")
        items.append(fix_item)
        encoders = runner.encoder_names()
        missing_enc = [e for e in ENCODERS if e not in encoders]
        items.append(
            _missing_item("encoders", list(ENCODERS), encoders, "; ".join(ENCODERS[e] for e in missing_enc), platform)
        )
        if "lavfi" in runner.device_names():
            items.append(Item("lavfi input", "ok", "present (svqa fix adds silent audio tracks with it)"))
        else:
            items.append(
                Item(
                    "lavfi input",
                    "fail",
                    "missing: svqa fix cannot add a silent track (audio.present)",
                    ["install a full FFmpeg build (with libavdevice):", *install_lines(platform), USE_IT],
                )
            )
        ready = not any(i.failed for i in items[before:])
        ready = ready and all(f in filters for f in PIPELINE_NEEDS)
        if ready:
            items.append(_pipeline_item(runner, platform))
        else:
            items.append(Item("pipeline test", "skip", "skipped until the FFmpeg items above pass"))
    else:
        items.append(Item("FFmpeg features", "skip", "skipped until the ffmpeg and ffprobe items above pass"))

    items.append(_builtin_item())
    items += _folder_items(profile_dirs)
    items.append(_formats_item())
    return items


_MARKS = {"ok": "pass", "fail": "error", "warn": "warn", "skip": "skip"}


def render(items: Sequence[Item], style: Style | None = None) -> str:
    """The human report: one line per item, the fix under every problem, and a verdict."""
    style = style or Style()
    width = max((len(i.name) for i in items), default=0)
    mark_width = 1 if style.unicode else 4
    pad = " " * (2 + mark_width + 1 + width + 2)
    lines = [f"svqa {__version__} doctor: what svqa check and svqa fix need (read-only, offline)", ""]
    for item in items:
        lines.append(f"  {style.mark(_MARKS[item.status])} {item.name:<{width}}  {item.detail}")
        for n, text in enumerate(item.fix):
            lines.append(f"{pad}{'fix: ' if n == 0 else '     '}{text}")
    lines.append("")
    failed = [i for i in items if i.failed]
    if failed:
        lines.append(
            style.paint(f"not ready: {len(failed)} problem{'s' if len(failed) != 1 else ''}", "31;1")
            + f". Apply the fix under each {style.mark('error')}, then run svqa doctor again."
        )
        lines.append(f"more help: {DOCS_URL}troubleshooting.md")
    else:
        warned = any(i.status == "warn" for i in items)
        lines.append(
            style.paint("ready", "32;1")
            + ": svqa check and svqa fix can use every feature"
            + (f" (see the {style.mark('warn')} above)." if warned else ".")
        )
        lines.append(f"next: svqa check clip.mp4   (first steps: {DOCS_URL}setup.md)")
    return "\n".join(lines) + "\n"
