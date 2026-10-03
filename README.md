# social-video-qa

**Catch video upload problems before the platform does.** `svqa` checks your videos against the published specs for
Instagram (Graph API), Meta ads, App Store app previews, YouTube Shorts and TikTok. It then fixes what can be fixed
safely, offline, with FFmpeg.

[![CI](https://github.com/deegitech/social-video-qa/actions/workflows/ci.yml/badge.svg)](https://github.com/deegitech/social-video-qa/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10 to 3.14](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![Needs FFmpeg 4.4+](https://img.shields.io/badge/needs-FFmpeg%204.4%2B-orange)](https://ffmpeg.org/download.html)
[![Works offline](https://img.shields.io/badge/network-none-brightgreen)](#security-model)

```console
$ svqa check teaser.mp4 -p instagram-reels-api
teaser.mp4 · instagram-reels-api · FAIL (2 errors, 4 warnings)
  video h264 High@4.0 · 1080x1920 · 30 fps CFR · 5.8 Mbps · yuv420p · B-frames
  audio aac LC · 48 kHz · 2 ch · 192 kbps · -6.3 LUFS · TP -5.3 dBTP · hiss -70
  file  mp4 · 12.00 s · 9.0 MB · 6.0 Mbps · moov last · 2 edit list(s) · 9:16
  ✗ container.faststart  moov box is after mdat: the whole file must be read before playback or processing can start
  ✗ container.edit_list  edit list (elst) in 2 track(s): video, audio
  ! video.b_frames       B-frames present (from packet timestamps, x264 bframes=3)
                         note: Not in Meta's spec. Without B-frames an MP4 needs no edit list to start at time zero.
  ! audio.bitrate        audio bitrate 192 kbps is above the maximum 130 kbps
                         note: Meta writes '128kbps'. Treated as a ceiling with 2 kbps of slack, because AAC encoders asked for 128k often measure slightly above it.
  ! audio.loudness       integrated loudness -6.3 LUFS: too loud by 7.7 LU (target -14 +/- 1 LUFS)
                         note: Common streaming target; Meta publishes no loudness requirement.
  ! metadata.location    location metadata present (location, location-eng): it can reveal where the video was recorded
  fix: svqa fix teaser.mp4 -p instagram-reels-api

1 file x 1 profile: 0 passed (0 with warnings), 1 failed · 2 errors, 4 warnings

$ svqa fix teaser.mp4 -p instagram-reels-api --apply
teaser.mp4 -> teaser.instagram-reels-api.mp4 · instagram-reels-api · WRITTEN (full)
  fails now: container.faststart (error), container.edit_list (error), video.b_frames (warn), audio.bitrate (warn), audio.loudness (warn), metadata.location (warn)
  video: re-encode with libx264 high, CRF 20, max 8000 kbps, preset medium, no B-frames, closed GOP every 60 frames, 1080x1920 (size kept), 30 fps CFR
  audio: re-encode AAC 128 kbps, 48000 Hz, 2 ch, two-pass loudnorm to -14 LUFS / -1.5 dBTP
  container: MP4, moov first, no edit list, metadata stripped
  verified: PASS
  wrote teaser.instagram-reels-api.mp4 (5.8 s)
```

## First 15 minutes

1. **Install** FFmpeg and svqa. [docs/setup.md](docs/setup.md) walks through every step from zero on macOS and Linux,
   including Fedora, old distributions and machines without root.
2. **Run `svqa doctor`.** It checks ffmpeg and ffprobe, their version, the demuxers, filters and encoders svqa uses, a
   0.5 s test encode, and your profiles. It prints `✓` or `✗` for each item and the exact fix under every `✗`.
3. **Check, then plan a fix without writing anything:** `svqa check clip.mp4`, then
   `svqa fix clip.mp4 -p instagram-reels-api`. Add `--apply` when the plan looks right.
4. **Stuck?** [docs/troubleshooting.md](docs/troubleshooting.md) lists every error message with its meaning and fix,
   plus upload errors from the platforms that svqa can or cannot prevent. Most errors also print a one-line `hint:`.

No account, API key or token is needed at any point.

## Why

Video upload pipelines fail late, and they fail vaguely.

- Instagram's Graph API accepts the upload, then reports a generic "unsupported format" error. The spec it checks
  against includes container details that no video player shows you, such as where the `moov` box sits and whether the
  file has edit lists.
- App Store Connect turns down an app preview that is 31 seconds long, a few pixels off one of Apple's accepted sizes, or
  above 30 fps. Many developers also report rejections for previews without an audio track.
- Other problems never fail at all. A 16:9 clip is cropped in a 9:16 feed. A variable frame rate drifts out of sync after
  the platform re-encodes it. A hot master is turned down and a quiet one sounds weak. A black first frame becomes your
  cover. And the GPS location where the video was shot rides along in the metadata.

`svqa` checks all of this locally before you upload, in a second or two per file. Every result names the rule it comes
from and whether that rule is documented by the platform, observed in practice, or a convention.

## Features

- **38 checks** on container layout (`moov` position, edit lists, tracks), video (codec, profile, level, size, aspect,
  frame rate, constant frame rate, bitrate, B-frames, closed GOPs, keyframe interval), audio (codec, sample rate,
  channels, bitrate, EBU R128 loudness, true peak, a hiss heuristic), opening frames (black or frozen) and privacy (GPS
  location tags). See [docs/checks.md](docs/checks.md).
- **7 built-in profiles**, all data: each rule links to its source and is marked `documented`, `observed` or
  `convention`. See [docs/profiles.md](docs/profiles.md).
- **Your own profiles** in JSON, YAML or TOML. They can extend a built-in profile and change only what differs.
- **`svqa fix`** picks the cheapest repair that works: a remux (no quality loss), an audio-only re-encode, or a full
  re-encode. It handles two-pass loudness normalisation, faststart, no edit lists, constant frame rate, pad/crop/blur
  to a new aspect ratio, trimming, and metadata stripping. It re-checks the result before publishing it, and it is a dry
  run unless you pass `--apply`.
- **Human, JSON and JUnit XML** output, so it drops into CI.
- **No Python dependencies, no network.** Only `ffmpeg` and `ffprobe`, run with CPU-friendly defaults (2 threads, `nice`).

## Install

You need Python 3.10 or newer, git (for the install from GitHub) and FFmpeg 4.4 or newer (with libx264 for `fix`):

```sh
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian / Ubuntu
```

Then install the tool. It is not on PyPI yet, so install it from GitHub:

```sh
pipx install git+https://github.com/deegitech/social-video-qa
# or, inside a virtualenv, with YAML profile support:
pip install "social-video-qa[yaml] @ git+https://github.com/deegitech/social-video-qa"

svqa doctor     # checks FFmpeg, its encoders and filters, and your profiles; prints the fix for each problem
```

The full walkthrough, with Fedora, static builds, an install without git, older Pythons and CI, is in
[docs/setup.md](docs/setup.md).

## Quickstart

No footage at hand? FFmpeg can make a 12-second test clip:

```sh
ffmpeg -f lavfi -i testsrc2=size=1080x1920:rate=30:duration=12 \
       -f lavfi -i sine=frequency=440:sample_rate=48000:duration=12 \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest clip.mp4
```

Then:

```sh
svqa check clip.mp4                              # which platforms is this file ready for?
svqa check clip.mp4 -p instagram-reels-api       # detailed report for one platform
svqa fix clip.mp4 -p instagram-reels-api         # show the fix plan (writes nothing)
svqa fix clip.mp4 -p instagram-reels-api --apply # write clip.instagram-reels-api.mp4, verified
svqa check out/ -p tiktok --junit qa.xml         # every video in a folder, JUnit report for CI
svqa profiles show app-store-preview-iphone      # what a profile checks, and why
```

Without `-p`, `svqa check` runs every built-in profile and prints one line per profile:

```console
$ svqa check teaser.mp4
teaser.mp4
  ✗ app-store-preview-ipad       FAIL (2 errors, 4 warnings): duration, bitrate.total, video.size, audio.bitrate, audio.loudness, metadata.location
  ✗ app-store-preview-iphone     FAIL (2 errors, 4 warnings): duration, bitrate.total, video.size, audio.bitrate, audio.loudness, metadata.location
  ✗ instagram-reels-api          FAIL (2 errors, 4 warnings): container.faststart, container.edit_list, video.b_frames, audio.bitrate, audio.loudness, metadata.location
  ✗ instagram-story-api          FAIL (2 errors, 4 warnings): container.faststart, container.edit_list, video.b_frames, audio.bitrate, audio.loudness, metadata.location
  ! meta-ads-instagram           PASS with warnings (3 warnings): container.faststart, audio.loudness, metadata.location
  ! tiktok                       PASS with warnings (2 warnings): audio.loudness, metadata.location
  ! youtube-shorts               PASS with warnings (4 warnings, 2 notes): container.faststart, container.edit_list, audio.loudness, metadata.location
```

## Built-in profiles

| Profile | For | Key limits |
| --- | --- | --- |
| `instagram-reels-api` | Reels published with the Instagram Graph API | 3 s to 15 min, 300 MB, H.264/HEVC, 23 to 60 fps, width up to 1920, closed GOP, no edit lists, moov first, AAC up to 48 kHz |
| `instagram-story-api` | Stories published with the Instagram Graph API | Same as Reels, 3 to 60 s, 100 MB |
| `meta-ads-instagram` | Meta ads in Instagram Reels/Stories placements | 15 s maximum (observed, warns), 9:16, 1440x2560 or 1080x1920 recommended, AAC 128 kbps or more |
| `app-store-preview-iphone` | App previews for 6.9", 6.5", 6.3" and 6.1" iPhones | 886x1920 or 1920x886, 15 to 30 s, up to 30 fps, H.264 up to High@4.0, about 10 to 12 Mbps, stereo AAC, 500 MB |
| `app-store-preview-ipad` | App previews for 13", 12.9", 11" and 10.5" iPads | 1200x1600 or 1600x1200, otherwise as iPhone |
| `youtube-shorts` | Uploads meant to be classified as Shorts | Square or vertical, up to 3 min, YouTube's recommended upload settings as warnings |
| `tiktok` | TikTok Content Posting API and app uploads | 23 to 60 fps, 360 to 4096 px, up to 10 min (3 min warns), 4 GB |

Every rule, its source link, its basis and the uncertainty notes are listed in [docs/profiles.md](docs/profiles.md).
Platforms change their specs without notice; each source shows the date it was checked.

## Commands

### `svqa check PATH... [-p PROFILE]...`

Read-only. `PATH` can be files or folders (searched recursively for `.mp4 .mov .m4v .webm .mkv .avi .ts .3gp`).

| Option | Meaning |
| --- | --- |
| `-p, --profile` | Profile ID or file, repeatable. Default: every built-in profile. |
| `--profile-dir DIR` | Extra folder with profile files, repeatable (also `$SVQA_PROFILE_PATH`). |
| `--strict` | Treat warnings as failures (exit 1). |
| `--skip CHECK` / `--only CHECK` | Turn checks off, or run only some. Globs work: `--skip 'audio.*'`. |
| `--fast` | Skip the decoding analyses (frame timing, loudness, hiss, opening frames). |
| `--format human\|json\|junit` | What to print on stdout. |
| `--json FILE`, `--junit FILE` | Also write a JSON or JUnit XML report. |
| `-v` / `-q` | Show passing checks too / only files with problems. |
| `--color auto\|always\|never` | Colour in human output (default `auto`; `NO_COLOR` also turns it off). |
| `--threads N`, `--nice N`, `--timeout S` | Resource limits for FFmpeg (defaults: 2, 10, 600). `--timeout` bounds each analysis step. |

### `svqa fix PATH... -p PROFILE [--apply]`

Writes a fixed copy next to each input (`clip.<profile>.mp4`) or into `-O DIR`, where files found in a folder keep
their sub-folder (`previews/a/clip.mp4` becomes `DIR/a/clip.<profile>.mp4`). The input is never modified, and two inputs
that would write the same output (say `intro.mov` and `intro.mp4`) are refused rather than overwriting each other.
**Without `--apply` it only prints the plan.**

| Option | Meaning |
| --- | --- |
| `--apply` | Actually write the output. |
| `-o FILE` / `-O DIR` | Output file (one input) or output folder. |
| `--mode auto\|remux\|audio\|full` | `auto` (default) picks the cheapest mode that fixes every fixable failure. |
| `--fit pad\|crop\|blur` | How to reach a different aspect ratio (default: profile setting, then `pad`). |
| `--trim` | Cut videos that are longer than the profile allows (with a 0.3 s audio fade-out). |
| `--preset`, `--crf` | x264 speed/quality trade-off for quality-based profiles. |
| `--no-loudnorm` | Leave loudness alone. |
| `--keep-metadata` | Keep container metadata (by default it is stripped, including GPS location). |
| `--force` | Re-encode even if up to date, and replace an existing output this tool did not write. |
| `--keep-failed` | Keep an output that fails verification as `*.failed.mp4` for inspection. |
| `--no-manifest` | Do not keep the `.svqa-manifest.json` journal (and take no folder lock); existing outputs then need `--force`. |
| `--show-commands` | Print the exact `ffmpeg` commands. |
| `--format human\|json`, `--json FILE` | What to print on stdout; also write the JSON result to `FILE`. |
| `-q` | Less output. |

`fix` also accepts `--skip`/`--only`/`--strict`, `--profile-dir`, `--color` and the resource options of `check`. With
`--strict`, a warning that `fix` cannot repair (such as hiss) refuses the fix up front, because the output could not
pass.

How the modes, encode settings and safety steps work is described in [docs/fix.md](docs/fix.md).

### `svqa profiles [list | show ID | validate FILE...]`

Lists profiles (built-in and your own), shows one resolved profile with its sources (`--format json|markdown` also
work), or validates profile files.

### `svqa probe PATH...`

Prints every measurement as JSON without applying a profile. Handy for debugging and for building your own rules.

### `svqa doctor [--profile-dir DIR]...`

Read-only, offline checks of everything `check` and `fix` need, in this order: Python, where `ffmpeg` and `ffprobe` were
found (on `PATH`, or through `SVQA_FFMPEG` / `SVQA_FFPROBE`), their version (4.4 or newer), whether each one really is
the program it should be (a variable pointing `ffprobe` at `ffmpeg` is caught here), whether they come from the same
install, the demuxers, the filters for `check` and for `fix`, the encoders, the lavfi input, and a pipeline
test. The pipeline test encodes 0.5 s of FFmpeg's built-in test picture and tone with libx264, AAC, `loudnorm` and
`ebur128` into FFmpeg's null output, so nothing is written. Then come the built-in profiles and your profile folders
(`--profile-dir`, `SVQA_PROFILE_PATH`).

Each item gets `✓` (ready), `✗` (a problem, with the exact fix printed under it: the install command for your system,
the variable to set or the file to correct), `!` (works, worth a look) or `-` (optional). The exit code is 0 when there
is no `✗`, otherwise 1. Sample output is in [docs/setup.md](docs/setup.md#3-verify-everything-with-svqa-doctor).

## Configuration

### Your own profiles

Create a file, extend a built-in profile and change only what differs. `null` removes an inherited rule.

```json
{
  "id": "brand-reels",
  "extends": "instagram-reels-api",
  "title": "Our Reels",
  "rules": {
    "video.size": {"allowed": ["1080x1920"], "severity": "error"},
    "video.fps": {"min": 30, "max": 30},
    "audio.loudness": {"tolerance_lu": 0.5},
    "audio.silence": null
  },
  "fix": {"fps": 30, "video": {"crf": 18}}
}
```

```sh
svqa check clip.mp4 -p ./brand-reels.json
svqa check clip.mp4 -p brand-reels --profile-dir ./profiles   # or set SVQA_PROFILE_PATH
```

The full schema, every check's parameters and more examples are in [docs/writing-profiles.md](docs/writing-profiles.md)
and [examples/](examples/).

### Environment variables

| Variable | Effect |
| --- | --- |
| `SVQA_PROFILE_PATH` | Extra profile folders, separated like `PATH`. Searched after `--profile-dir`, before the built-ins. |
| `SVQA_FFMPEG`, `SVQA_FFPROBE` | Use these binaries instead of the ones on `PATH`. |
| `NO_COLOR` | Disable colour in human output. |

### Exit codes

`0` everything passed, `1` a check or fix failed, `2` usage or profile error, `3` FFmpeg missing. `svqa doctor` exits
`0` when ready and `1` otherwise. When a run fails, most messages end with a one-line `hint:` (in JSON: `"hint"`), and
[docs/troubleshooting.md](docs/troubleshooting.md) lists every message with its meaning and fix.

## Examples

### GitHub Actions

```yaml
- run: sudo apt-get update && sudo apt-get install -y ffmpeg
- run: pipx install git+https://github.com/deegitech/social-video-qa@v0.1.0
- run: svqa doctor
- run: svqa check exports/ -p instagram-reels-api -p tiktok --junit svqa-report.xml
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: svqa-report
    path: svqa-report.xml
```

A complete workflow is in [examples/github-actions.yml](examples/github-actions.yml).

### Fix a whole folder for App Store previews

```sh
svqa fix previews/ -p app-store-preview-iphone -O upload/            # review the plan
svqa fix previews/ -p app-store-preview-iphone -O upload/ --apply --trim --fit blur
```

The outputs keep the folder layout of `previews/`. Re-running the second command skips outputs that are already up to
date: same source, same profile, same settings.

Files that already pass are reported as `NOTHING TO DO` and are **not** copied into `-O`, so `upload/` holds only the
files that needed a fix. Upload the passing originals alongside them.

### Python API (provisional)

```python
from social_video_qa import check_file, load_profile

report = check_file("clip.mp4", load_profile("youtube-shorts"))
if not report.ok:
    for result in report.failures():
        print(result.id, result.level, result.message)
```

## Security model

`svqa` handles media from anywhere, so it is built to be safe to point at untrusted files and to be boring to run.

- **No network, no credentials.** The tool never opens a network connection and needs no accounts, API keys or tokens,
  so there are no secrets to leak. It sends no telemetry.
- **Hardened FFmpeg calls.** Every FFmpeg/FFprobe call uses an argument list (never a shell) with stdin closed. Your
  file is opened only as a `file:` URL with `-protocol_whitelist file` and a demuxer whitelist (MP4/MOV, Matroska/WebM,
  AVI, MPEG-TS). A crafted HLS playlist or `ffconcat` script disguised as a video cannot make FFmpeg fetch URLs or read
  other local files; it is refused (there is a test for this).
- **Read-only by default.** `check`, `probe`, `profiles` and `doctor` never write. `fix` is a dry run unless you pass
  `--apply`.
- **Never in place, never half-written.** `fix` refuses to write over its input. It encodes into a temporary file in the
  destination folder, re-checks it against the profile, and moves it into place with an atomic rename only if no
  error-level check fails. Temporary files are removed on failure or Ctrl-C.
- **No clobbering.** An existing output is replaced only if this tool wrote it (its SHA-256 is in the folder's
  `.svqa-manifest.json`) or you pass `--force`.
- **Journaled and idempotent.** Before encoding, `fix` records the intent (source hash, profile hash, recipe) in the
  manifest, and afterwards the result with the output hash. A re-run with the same inputs is skipped. An interrupted run
  is visible as `encoding` or `interrupted`, and the journal keeps proving ownership of the previous output. An output
  written from one source is not replaced by a different source without `--force`. A lock file stops two runs from
  writing the same folder at once (not with `--no-manifest`).
- **Privacy.** `fix` strips container and stream metadata by default: recording location, device make and model,
  creation time. `check` warns about location tags.
- **Polite resource use.** FFmpeg runs with 2 threads per stage and `nice 10` by default, and analysis steps have a
  timeout. Encodes have none, so bound `svqa fix` with `timeout(1)` or container limits on a server.

What it does not protect you from: bugs in FFmpeg's own decoders. If you run `svqa` on files uploaded by strangers (for
example on a server), keep FFmpeg up to date and run it in a container or sandbox with no network access. Report
security problems as described in [SECURITY.md](SECURITY.md).

## Limitations and honest caveats

- **Specs move.** Platform pages change without notice and are sometimes vague. Values were checked on the dates shown
  in [docs/profiles.md](docs/profiles.md), and rules that come from practice rather than documentation are marked
  `observed`. Passing every check makes a rejection unlikely; it does not guarantee acceptance, and it says nothing about
  how a platform ranks your post.
- **The hiss index is a heuristic.** It is the loudness above 6 kHz, calibrated on game-trailer mixes at about -14 LUFS.
  Bright music or speech can trip it, and quiet masters can hide hiss.
- **Only the first video and the first audio stream are analysed.** Apple's "two mono tracks" stereo layout is reported
  as one channel.
- **Open-GOP detection depends on keyframe flags.** FFmpeg flags open-GOP I-frames as keyframes; files from encoders that
  do not are caught only by the x264/x265 settings string.
- **Any edit list counts**, including harmless ones, because the platforms' guidance does not distinguish them.
- **`fix` writes H.264 + AAC in MP4 only**, and needs an FFmpeg build with libx264. It cannot remove hiss, lengthen a short
  video, or replace a black or frozen opening. Pad, crop and blur are mechanical; for important videos, reframe by hand.
- **HDR is not tone-mapped.** iPhones record HLG HDR by default. When `fix` re-encodes such a video it converts it to 8-bit
  without tone mapping, so colours and highlights can look wrong (the plan says so). Export SDR from your editor before
  `fix`.
- **Visual content is not inspected** beyond the opening frames: no safe-zone, text or watermark checks.
- **Platforms.** Developed and tested on macOS with FFmpeg 8. CI runs the suite on Ubuntu, with the distribution's
  FFmpeg, and on macOS. FFmpeg 4.4 to 5.x should work but is not part of CI. Windows should work but is untested.

## FAQ

**Why not let the platform re-encode everything?** It will re-encode anyway. These checks are about what happens
*before* that: whether the upload is accepted at all, whether the picture is cropped or letterboxed, whether the audio
keeps its level and sync, and what metadata leaves your machine.

**Why does the Instagram profile warn about B-frames when Meta does not mention them?** Meta asks for no edit lists. MP4
files with B-frames normally need an edit list to start at time zero. Without B-frames, that need disappears, so `fix`
encodes without them. The rule is a warning and is marked `convention`.

**My file passes on Instagram but fails for App Store previews. Is that a bug?** Probably not. Each platform wants
different sizes, lengths and bitrates. Run `svqa check clip.mp4` without `-p` to see all of them at once.

**Can I make warnings fail CI?** Use `--strict`, or raise a rule's `severity` to `error` in your own profile.

**What is `.svqa-manifest.json`?** The journal `fix` keeps in each output folder: hashes and settings, used to skip
up-to-date files and to avoid overwriting files it did not write. It contains file names and hashes, not media. Add it to
`.gitignore` if you do not want it in version control, or use `--no-manifest`. Sources are identified by content hash and
by a hash of their path, so the manifest does not store your folder layout beyond the paths you typed.

**Does it upload anything or call any API?** No. It runs `ffmpeg` and `ffprobe` on your machine and nothing else.

**Something fails and I don't know why.** Run `svqa doctor` first. Then look the message up in
[docs/troubleshooting.md](docs/troubleshooting.md), which also covers upload errors from Instagram, Meta ads and App
Store Connect that are about the file, and the ones that are not.

## How it compares

| | `svqa` | MediaInfo / ffprobe | Online "video checkers" | Uploading and waiting |
| --- | --- | --- | --- | --- |
| Knows each platform's rules | yes, data-driven and sourced | no, raw data only | some | yes, eventually |
| Explains why, with the source | yes | no | rarely | cryptic error codes |
| Fixes safely and verifies | yes | no | sometimes | no |
| Your file stays on your machine | yes | yes | no | no |
| CI-friendly (JSON, JUnit, exit codes) | yes | partly | no | no |

## Contributing

Bug reports, spec updates and new profiles are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see
[SECURITY.md](SECURITY.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## About / Built by DEEGITECH

`social-video-qa` is built and maintained by [DEEGITECH](https://github.com/deegitech) (DEEGITECH Teknoloji ve Yazılım
Ltd. Şti.), a small game studio in Türkiye. We wrote it while shipping trailers, Reels and App Store previews for our
iOS game [Wide Molly Hooked](https://apps.apple.com/app/id6813081261) ([widemolly.com](https://widemolly.com/)), after
one too many uploads failed for reasons no video player shows. We open-sourced it so that other small teams don't have
to learn the same specs the hard way.

## License

[MIT](LICENSE) © 2026 DEEGITECH Teknoloji ve Yazılım Ltd. Şti.
