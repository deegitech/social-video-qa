# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-04

First public release.

### Added

- `svqa check`: 38 checks covering:
  - container layout: `moov` position, edit lists, stream count, track flags;
  - duration, file size and overall bitrate;
  - video: codec, profile, level, pixel format and chroma, scan type, frame size, resolution limits, aspect ratio,
    pixel aspect, rotation, frame rate, constant frame rate, bitrate, B-frames, closed GOPs, keyframe interval;
  - audio: presence, codec, profile, sample rate, channels, bitrate, EBU R128 loudness, true peak, hiss index, silence;
  - black or frozen opening frames, and location metadata.
- Seven built-in, data-driven profiles: `instagram-reels-api`, `instagram-story-api`, `meta-ads-instagram`,
  `app-store-preview-iphone`, `app-store-preview-ipad`, `youtube-shorts` and `tiktok`. Each rule has its source and a
  basis: documented, observed or convention.
- User profiles in JSON, YAML (optional PyYAML) or TOML (Python 3.11+, or the optional `toml` extra on 3.10), with
  `extends`, `null` deletion, strict validation and a search path (`--profile-dir`, `SVQA_PROFILE_PATH`).
- `svqa fix`:
  - a dry-run plan by default;
  - `remux`, `audio` and `full` modes, chosen automatically;
  - two-pass loudness normalisation, faststart, no edit lists, constant frame rate, cadence-aware frame-rate choice;
  - `pad`/`crop`/`blur` fitting, resampling of non-square pixels, trimming, AAC bitrate step-down and metadata
    stripping;
  - `-O DIR` keeps the sub-folders of folder inputs, and two inputs that would write the same output are refused;
  - verification before an atomic publish, plus a journaled, idempotent manifest with overwrite protection that also
    survives failed or interrupted re-runs and tells different sources apart.
- Human, JSON and JUnit XML reports. `svqa profiles` and `svqa probe`.
- `svqa doctor`: read-only, offline checks with one `✓`/`✗` line per item and the exact fix under every problem. It
  covers where the binaries come from (`PATH` or `SVQA_FFMPEG`/`SVQA_FFPROBE`), whether each one is the program it
  should be, their version and whether they belong to one install, the demuxers, the lavfi input, every filter and
  encoder `check` and `fix` use, a 0.5 s test encode into FFmpeg's null output, the built-in profiles and your profile
  folders (`--profile-dir`, `SVQA_PROFILE_PATH`). It exits with 0 when ready and 1 otherwise.
- One-line `hint:` fixes under known errors: FFmpeg builds without libx264 or a filter, a variable that points at the
  wrong FFmpeg program, broken FFmpeg libraries, binaries for another processor, incomplete MP4s
  (`moov atom not found`), timeouts, full disks, profile mistakes and refused fixes. JSON reports and
  `svqa fix --format json` carry it as `hint`. FFmpeg errors keep the line that names the cause, and the `svqa fix`
  plan says when this FFmpeg lacks an encoder it needs.
- `docs/setup.md` (setup from zero in about 15 minutes) and `docs/troubleshooting.md` (every error message with its
  meaning and fix, platform upload errors with the observed ones marked, and the exit codes).
- Hardened FFmpeg invocation: argument lists only, `file:` inputs with protocol and demuxer whitelists, timeouts,
  `nice`.

[Unreleased]: https://github.com/deegitech/social-video-qa/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deegitech/social-video-qa/releases/tag/v0.1.0
