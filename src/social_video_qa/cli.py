"""Command-line interface: ``svqa check | fix | profiles | probe | doctor``."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from . import __version__, doctor
from .checks import CheckReport, needs_for, run_checks, window_for
from .ffmpeg import FFmpegError, Runner, ToolNotFound
from .fix import MODES, FixError, FixOptions, FixResult, Manifest, describe_plan, fix_file, output_for
from .hints import hint_for
from .probe import NEEDS_ALL, MediaInfo, ProbeError, analyze
from .profiles import Profile, ProfileError, builtin_ids, list_profiles, load_profile
from .report import (
    Style,
    render_human,
    render_json,
    render_junit,
    render_profile_markdown,
    render_profile_text,
    write_text,
)
from .util import round_floats

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_ENV = 0, 1, 2, 3
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ts", ".3gp"}

EPILOG = """\
examples:
  svqa check clip.mp4                          which platforms is this file ready for?
  svqa check clip.mp4 -p instagram-reels-api   detailed report for one platform
  svqa check out/ -p tiktok --junit qa.xml     every video in a folder, JUnit for CI
  svqa fix clip.mp4 -p app-store-preview-iphone            show the fix plan (dry run)
  svqa fix clip.mp4 -p app-store-preview-iphone --apply    write the fixed copy
  svqa profiles show youtube-shorts            what a profile checks, and why

first time? run `svqa doctor`: it checks FFmpeg and prints the fix for anything missing

exit codes: 0 ok, 1 a check or fix failed (doctor: not ready), 2 usage or profile error, 3 ffmpeg/ffprobe missing
docs: https://github.com/deegitech/social-video-qa
help: https://github.com/deegitech/social-video-qa/blob/main/docs/troubleshooting.md
"""


# ---------------------------------------------------------------------- argument parsing
def _runtime_args(p: argparse.ArgumentParser, *, timeout: bool = True) -> None:
    g = p.add_argument_group("resources")
    g.add_argument("--threads", type=int, default=2, metavar="N", help="FFmpeg threads (default: 2, CPU-friendly)")
    g.add_argument(
        "--nice",
        type=int,
        default=10,
        metavar="N",
        help="run FFmpeg with this nice value on POSIX (default: 10; 0 to disable)",
    )
    if timeout:  # doctor runs no analysis steps; its own FFmpeg calls stop after 30 to 60 s
        g.add_argument(
            "--timeout",
            type=float,
            default=600.0,
            metavar="S",
            help="timeout for each analysis step in seconds (default: 600)",
        )


def _profile_dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--profile-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directory with profile files (repeatable; also $SVQA_PROFILE_PATH)",
    )


def _filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="CHECK",
        help="disable a check, globs allowed (e.g. 'audio.*'); repeatable",
    )
    p.add_argument(
        "--only", action="append", default=[], metavar="CHECK", help="run only these checks, globs allowed; repeatable"
    )
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svqa",
        description="Check (and fix) videos against Instagram, TikTok, YouTube Shorts, Meta ads and App Store "
        "preview specs before you upload. Offline; needs ffmpeg and ffprobe.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    c = sub.add_parser("check", help="check videos against one or more profiles (read-only)")
    c.add_argument("paths", nargs="+", metavar="PATH", help="video files or directories (searched recursively)")
    c.add_argument(
        "-p",
        "--profile",
        action="append",
        default=[],
        metavar="PROFILE",
        help="profile ID or file (repeatable); default: all built-in profiles",
    )
    _filter_args(c)
    c.add_argument(
        "--fast",
        action="store_true",
        help="skip decoding-based analyses (frame timing, loudness, hiss, opening frames)",
    )
    c.add_argument(
        "--format", choices=("human", "json", "junit"), default="human", help="stdout format (default: human)"
    )
    c.add_argument("--json", metavar="FILE", help="also write the JSON report to FILE")
    c.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report to FILE (for CI)")
    c.add_argument("-v", "--verbose", action="store_true", help="show passing and skipped checks too")
    c.add_argument("-q", "--quiet", action="store_true", help="only show files with problems")
    c.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    _profile_dir_arg(c)
    _runtime_args(c)

    f = sub.add_parser(
        "fix",
        help="plan or write a fixed copy that passes a profile (dry run unless --apply)",
        description="Plan a fix (default, writes nothing) or, with --apply, write a fixed copy and verify it. "
        "The input is never modified.",
    )
    f.add_argument("paths", nargs="+", metavar="PATH", help="video files or directories")
    f.add_argument("-p", "--profile", required=True, metavar="PROFILE", help="profile ID or file")
    f.add_argument("--apply", action="store_true", help="actually write the output (default is a dry run)")
    out = f.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", metavar="FILE", help="output file (single input only)")
    out.add_argument(
        "-O",
        "--out-dir",
        metavar="DIR",
        help="output directory; files found in a folder keep their sub-folder (default: next to each input)",
    )
    f.add_argument(
        "--mode", choices=MODES, default="auto", help="auto (cheapest that works), remux, audio or full re-encode"
    )
    f.add_argument(
        "--fit",
        choices=("pad", "crop", "blur"),
        help="how to reach a different aspect ratio (default: profile, else pad)",
    )
    f.add_argument("--trim", action="store_true", help="cut videos that are longer than the profile allows")
    f.add_argument(
        "--preset",
        choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
        help="x264 preset (default: profile, else medium)",
    )
    f.add_argument("--crf", type=float, metavar="N", help="x264 CRF for quality-based profiles (lower = better/bigger)")
    f.add_argument("--no-loudnorm", dest="loudnorm", action="store_false", help="do not normalise loudness")
    f.add_argument(
        "--keep-metadata",
        action="store_true",
        help="keep container metadata (default: strip it, including GPS location)",
    )
    f.add_argument(
        "--force", action="store_true", help="re-encode even if up to date, and replace outputs this tool did not write"
    )
    f.add_argument("--keep-failed", action="store_true", help="keep an output that fails verification as *.failed.mp4")
    f.add_argument(
        "--no-manifest",
        dest="manifest",
        action="store_false",
        help="do not keep .svqa-manifest.json in the output directory",
    )
    f.add_argument("--show-commands", action="store_true", help="print the ffmpeg commands")
    f.add_argument("--format", choices=("human", "json"), default="human")
    f.add_argument("--json", metavar="FILE", help="also write the JSON result to FILE")
    f.add_argument("-q", "--quiet", action="store_true", help="less output")
    f.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    _filter_args(f)
    _profile_dir_arg(f)
    _runtime_args(f)

    pr = sub.add_parser("profiles", help="list, show or validate profiles")
    prs = pr.add_subparsers(dest="profiles_command", metavar="ACTION")
    pl = prs.add_parser("list", help="list available profiles")
    pl.add_argument("--format", choices=("human", "json"), default="human")
    _profile_dir_arg(pl)
    ps = prs.add_parser("show", help="show what a profile checks (resolved, with sources)")
    ps.add_argument("profile", metavar="PROFILE")
    ps.add_argument("--format", choices=("human", "json", "markdown"), default="human")
    _profile_dir_arg(ps)
    pv = prs.add_parser("validate", help="validate profile files")
    pv.add_argument("files", nargs="+", metavar="FILE")
    _profile_dir_arg(pv)

    pb = sub.add_parser("probe", help="print raw measurements as JSON (no profile)")
    pb.add_argument("paths", nargs="+", metavar="PATH")
    pb.add_argument("--fast", action="store_true", help="skip decoding-based analyses")
    _runtime_args(pb)

    d = sub.add_parser(
        "doctor",
        help="check FFmpeg, its encoders and filters, and your profiles; prints the fix for each problem",
        description="Read-only, offline checks of everything svqa check and svqa fix need: ffmpeg and ffprobe, "
        "their version, demuxers, filters and encoders, a 0.5 s synthetic test encode (written nowhere), and the "
        "built-in and your own profiles. Each problem comes with the exact fix. Exit code 0 when ready, 1 if not.",
    )
    _profile_dir_arg(d)
    d.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    _runtime_args(d, timeout=False)
    return parser


# ---------------------------------------------------------------------- helpers
def _err(msg: str) -> None:
    sys.stderr.write(f"svqa: {msg}\n")
    hint = hint_for(msg)
    if hint:
        sys.stderr.write(f"svqa: hint: {hint}\n")


def collect_inputs(paths: Sequence[str], *, skip_fix_outputs: bool = False) -> list[tuple[str, str]]:
    """Expand directories into the video files inside them (sorted, hidden files skipped).

    Returns ``(file, sub-folder)`` pairs; the sub-folder is relative to the directory argument the file was
    found in (``""`` for files given directly), so ``fix -O`` can mirror the folder layout.
    """
    files: list[tuple[str, str]] = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                outputs = Manifest(root).outputs() if skip_fix_outputs else set()
                rel = os.path.relpath(root, path)
                sub = "" if rel == os.curdir else rel
                for name in sorted(names):
                    if name.startswith(".") or os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                        continue
                    if name in outputs or ".failed." in name:
                        continue
                    files.append((os.path.join(root, name), sub))
        else:
            files.append((path, ""))
    return files


def collect_files(paths: Sequence[str], *, skip_fix_outputs: bool = False) -> list[str]:
    """Expand directories into the video files inside them (sorted, hidden files skipped)."""
    return [path for path, _sub in collect_inputs(paths, skip_fix_outputs=skip_fix_outputs)]


def _progress(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _runner(args: argparse.Namespace) -> Runner:
    return Runner.discover(threads=args.threads, nice=args.nice, timeout=args.timeout)


def _load_profiles(refs: Sequence[str], dirs: Sequence[str]) -> list[tuple[Profile, str]]:
    """Load the requested profiles; returns ``(profile, reference as typed)`` pairs, one per profile ID."""
    wanted = list(refs) or ["all"]
    out: list[tuple[Profile, str]] = []
    for ref in wanted:
        if ref == "all":
            out.extend((load_profile(pid, dirs), pid) for pid in builtin_ids())
        else:
            out.append((load_profile(ref, dirs), ref))
    seen: set[str] = set()
    unique = []
    for prof, ref in out:
        if prof.id not in seen:
            seen.add(prof.id)
            unique.append((prof, ref))
    return unique


# ---------------------------------------------------------------------- commands
def cmd_check(args: argparse.Namespace) -> int:
    try:
        profiles = _load_profiles(args.profile, args.profile_dir)
    except ProfileError as exc:
        _err(str(exc))
        return EXIT_USAGE
    try:
        runner = _runner(args)
    except ToolNotFound as exc:
        _err(str(exc))
        return EXIT_ENV
    needs: set[str] = set()
    for prof, _ref in profiles:
        needs |= needs_for(prof.rules, args.skip, args.only)
    if args.fast:
        needs = set()
    window = max(window_for(p.rules) for p, _ref in profiles)
    files = collect_files(args.paths)
    if not files:
        _err("no video files found")
        return EXIT_USAGE
    reports: list[CheckReport] = []
    media: dict[str, MediaInfo] = {}
    for path in files:
        try:
            m = analyze(path, runner, needs, start_window_s=window)
        except ProbeError as exc:
            for prof, ref in profiles:
                reports.append(
                    CheckReport(
                        file=path,
                        profile=prof.id,
                        profile_title=prof.title,
                        error=str(exc),
                        strict=args.strict,
                        profile_ref=ref,
                    )
                )
            continue
        media[path] = m
        for prof, ref in profiles:
            report = run_checks(
                m,
                prof.rules,
                file=path,
                profile=prof.id,
                profile_title=prof.title,
                skip=args.skip,
                only=args.only,
                strict=args.strict,
            )
            report.profile_ref = ref  # what the user typed, so the suggested `svqa fix` command works as shown
            reports.append(report)
    version = runner.version_label()
    if args.json:
        write_text(args.json, render_json(reports, media, ffmpeg_version=version, strict=args.strict))
    if args.junit:
        write_text(args.junit, render_junit(reports, strict=args.strict))
    if args.format == "json":
        sys.stdout.write(render_json(reports, media, ffmpeg_version=version, strict=args.strict))
    elif args.format == "junit":
        sys.stdout.write(render_junit(reports, strict=args.strict))
    else:
        style = Style.for_stream(sys.stdout, args.color)
        text = render_human(
            reports, media, style=style, verbose=args.verbose, quiet=args.quiet, profile_dirs=args.profile_dir
        )
        sys.stdout.write(text)
    return EXIT_OK if all(r.ok for r in reports) else EXIT_FAIL


def _render_fix_human(results: Sequence[FixResult], args: argparse.Namespace, style: Style) -> str:
    lines: list[str] = []
    for res in results:
        plan = res.plan
        mode = plan.mode if plan else "-"
        status = {
            "planned": style.paint("PLAN", "36;1"),
            "nothing-to-do": style.paint("NOTHING TO DO", "32;1"),
            "up-to-date": style.paint("UP TO DATE", "32;1"),
            "written": style.paint("WRITTEN", "32;1"),
            "failed": style.paint("FAILED VERIFICATION", "31;1"),
            "refused": style.paint("REFUSED", "31;1"),
            "error": style.paint("ERROR", "31;1"),
        }[res.status]
        target = f" -> {res.output}" if res.output and res.status not in ("error",) else ""
        mode_text = f" ({mode})" if plan and plan.mode != "none" else ""
        lines.append(
            style.bold(f"{res.source}{target}") + f" · {plan.profile if plan else args.profile} · {status}{mode_text}"
        )
        if res.before is not None and not args.quiet:
            failing = [r for r in res.before.results if r.failed and r.level in ("error", "warn")]
            if failing:
                lines.append("  fails now: " + ", ".join(f"{r.id} ({r.level})" for r in failing))
        if plan is not None and plan.mode != "none" and not args.quiet:
            for text in describe_plan(plan):
                lines.append(f"  {text}")
            for note in plan.notes:
                lines.append(style.dim(f"  note: {note}"))
        if plan is not None and plan.unfixed and not args.quiet:
            for r in plan.unfixed:
                lines.append(
                    f"  {style.mark('warn' if r.level == 'warn' else 'error')} not fixable: {r.id}: {r.message}"
                )
        if res.after is not None:
            after = res.after
            word = "PASS" if after.ok else "FAIL"
            warned = [r.id for r in after.failures(("warn",))]
            extra = f" (warnings: {', '.join(warned)})" if warned else ""
            lines.append(f"  verified: {word}{extra}")
            for r in after.failures(("error",)):
                lines.append(f"    {style.mark('error')} {r.id}: {r.message}")
        if args.show_commands:
            for cmd in res.commands:
                lines.append(style.dim("  $ " + shlex.join(cmd)))
        if res.message:
            # FFmpeg's error output can span several lines; indent them under the first so the report keeps its shape.
            first, *more = res.message.splitlines()
            took = f" ({res.elapsed_s:.1f} s)" if res.elapsed_s and res.status == "written" else ""
            lines.append(f"  {first}{took}")
            lines.extend(f"    {line.strip()}" for line in more if line.strip())
            hint = hint_for(res.message) if not res.ok else None
            if hint:
                lines.append(style.dim(f"  hint: {hint}"))
        if res.status == "planned":
            lines.append(style.dim("  re-run with --apply to write it"))
        lines.append("")
    done = sum(1 for r in results if r.ok)
    lines.append(f"{len(results)} file{'s' if len(results) != 1 else ''}: {done} ok, {len(results) - done} not ok")
    return "\n".join(lines) + "\n"


def cmd_fix(args: argparse.Namespace) -> int:
    if args.profile == "all":
        _err("fix needs one profile, not 'all'")
        return EXIT_USAGE
    try:
        profile = load_profile(args.profile, args.profile_dir)
    except ProfileError as exc:
        _err(str(exc))
        return EXIT_USAGE
    inputs = collect_inputs(args.paths, skip_fix_outputs=True)
    if not inputs:
        _err("no video files found")
        return EXIT_USAGE
    if args.output and len(inputs) != 1:
        _err("--output needs exactly one input file; use --out-dir for several")
        return EXIT_USAGE
    try:
        runner = _runner(args)
    except ToolNotFound as exc:
        _err(str(exc))
        return EXIT_ENV
    opts = FixOptions(
        apply=args.apply,
        output=args.output,
        out_dir=args.out_dir,
        mode=args.mode,
        fit=args.fit,
        trim=args.trim,
        preset=args.preset,
        crf=args.crf,
        loudnorm=args.loudnorm,
        keep_metadata=args.keep_metadata,
        force=args.force,
        keep_failed=args.keep_failed,
        manifest=args.manifest,
        strict=args.strict,
        skip=args.skip,
        only=args.only,
    )
    log = _progress if args.format == "human" and not args.quiet and args.apply else None
    results: list[FixResult] = []
    claimed: dict[str, str] = {}  # output -> the input that writes it
    sources: set[str] = set()
    for path, sub in inputs:
        real = os.path.realpath(path)
        if real in sources:
            continue  # the same file was named twice
        sources.add(real)
        # With --out-dir, files found inside a folder keep their sub-folder, so a/clip.mp4 and b/clip.mp4
        # cannot overwrite each other.
        file_opts = replace(opts, out_dir=os.path.join(opts.out_dir, sub)) if opts.out_dir and sub else opts
        output = output_for(path, profile, file_opts)
        key = os.path.realpath(output)
        if sys.platform in ("darwin", "win32"):  # case-insensitive file systems by default
            key = key.casefold()
        other = claimed.setdefault(key, path)
        if other != path:
            message = (
                f"{output} is also the output for {other}; rename one of them, or fix this one on its own "
                "with -o FILE or another --out-dir"
            )
            results.append(FixResult(path, output, "refused", message))
            continue
        if log is not None:
            log(f"{path}:")
        try:
            results.append(fix_file(path, profile, runner, file_opts, log=log))
        except (FFmpegError, FixError, OSError) as exc:
            results.append(FixResult(path, output, "error", str(exc)))
    if args.json:
        write_text(args.json, _fix_json(results, args))
    if args.format == "json":
        sys.stdout.write(_fix_json(results, args))
    else:
        sys.stdout.write(_render_fix_human(results, args, Style.for_stream(sys.stdout, args.color)))
    return EXIT_OK if all(r.ok for r in results) else EXIT_FAIL


def _fix_json(results: Sequence[FixResult], args: argparse.Namespace) -> str:
    doc = {
        "schema": "social-video-qa/fix@1",
        "tool": {"name": "social-video-qa", "version": __version__},
        "apply": bool(args.apply),
        "ok": all(r.ok for r in results),
        "results": [r.to_dict() for r in results],
    }
    return json.dumps(round_floats(doc), indent=2, ensure_ascii=False) + "\n"


def cmd_profiles(args: argparse.Namespace) -> int:
    action = args.profiles_command or "list"
    dirs = getattr(args, "profile_dir", [])
    if action == "list":
        refs = list_profiles(dirs)
        if getattr(args, "format", "human") == "json":
            sys.stdout.write(json.dumps([r.__dict__ for r in refs], indent=2) + "\n")
            return EXIT_OK
        width = max(len(r.id) for r in refs) if refs else 10
        for r in refs:
            origin = "" if r.origin == "builtin" else f"  [{r.origin}]"
            sys.stdout.write(f"{r.id:<{width}}  {r.title}{origin}\n")
        return EXIT_OK
    if action == "show":
        try:
            prof = load_profile(args.profile, dirs)
        except ProfileError as exc:
            _err(str(exc))
            return EXIT_USAGE
        if args.format == "json":
            sys.stdout.write(json.dumps(prof.to_dict(), indent=2, ensure_ascii=False) + "\n")
        elif args.format == "markdown":
            sys.stdout.write(render_profile_markdown(prof))
        else:
            sys.stdout.write(render_profile_text(prof))
        return EXIT_OK
    # validate
    bad = 0
    for path in args.files:
        try:
            load_profile(path, dirs)
            sys.stdout.write(f"ok       {path}\n")
        except ProfileError as exc:
            bad += 1
            sys.stdout.write(f"invalid  {path}\n  {exc}\n")
    return EXIT_OK if not bad else EXIT_USAGE


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        runner = _runner(args)
    except ToolNotFound as exc:
        _err(str(exc))
        return EXIT_ENV
    out: dict[str, Any] = {}
    status = EXIT_OK
    for path in collect_files(args.paths):
        try:
            out[path] = analyze(path, runner, frozenset() if args.fast else NEEDS_ALL).to_dict()
        except ProbeError as exc:
            out[path] = {"error": str(exc), "hint": hint_for(str(exc))}
            status = EXIT_FAIL
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return status


def cmd_doctor(args: argparse.Namespace) -> int:
    items = doctor.diagnose(args.profile_dir, threads=args.threads, nice=args.nice)
    sys.stdout.write(doctor.render(items, Style.for_stream(sys.stdout, args.color)))
    return EXIT_FAIL if any(item.failed for item in items) else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    handlers = {"check": cmd_check, "fix": cmd_fix, "profiles": cmd_profiles, "probe": cmd_probe, "doctor": cmd_doctor}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
