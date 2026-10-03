# Setup from zero (about 15 minutes)

This guide takes a machine with nothing installed to a verified `svqa`, a first check and a first fix, step by step.
svqa runs offline and needs no account, API key or token. The only things to set up are Python, FFmpeg and the tool
itself (fetched with git, or as an archive without it), and you can copy the commands below as they are. Nothing gets
uploaded anywhere.

Package names and installer menus change over time. Where this guide names one, the name may differ on your system.
When something goes wrong, [troubleshooting.md](troubleshooting.md) lists every error message with its fix.

| Step | What you do | Time |
| --- | --- | --- |
| [0](#0-prerequisites) | Check the prerequisites | 1 min |
| [1](#1-install-ffmpeg) | Install FFmpeg (with libx264) | 3 min |
| [2](#2-install-svqa) | Install svqa | 2 min |
| [3](#3-verify-everything-with-svqa-doctor) | Verify everything with `svqa doctor` | 1 min |
| [4](#4-your-first-check) | Check a video | 3 min |
| [5](#5-your-first-fix-dry-run-first) | Plan a fix, then write it | 3 min |
| [6](#6-pick-your-profiles) | Pick the profiles for your uploads | 2 min |
| [7](#7-put-it-in-front-of-your-uploads) | Put it in front of your uploads (scripts, CI, servers) | optional |

## 0. Prerequisites

| You need | Why | Check it with |
| --- | --- | --- |
| macOS or Linux | Developed on macOS, tested on Ubuntu and macOS. Windows should work but is untested. | |
| Python 3.10 or newer | svqa is a Python program with no Python dependencies. | `python3 --version` |
| FFmpeg 4.4 or newer, with libx264 | `ffprobe` measures, `ffmpeg` runs the analyses and writes the fixes. libx264 is needed only when `svqa fix` re-encodes video. | `ffmpeg -hide_banner -version` |
| pipx (recommended) | Installs svqa in its own environment and puts `svqa` on your `PATH`. | `pipx --version` |
| git | The install commands below fetch svqa from GitHub with git. Step 2 also shows an install that needs no git. | `git --version` |
| Free disk space | `svqa fix` writes a new copy of each video it fixes, through a temporary file in the same folder. | |
| Accounts, roles, paid plans, API keys | **None.** svqa never goes online. | |

If something is missing:

- **git.** macOS: `xcode-select --install` (Apple's Command Line Tools include git) or `brew install git`.
  Debian/Ubuntu: `sudo apt install git`. Fedora: `sudo dnf install git`. Slim containers often have no git; use the
  archive install in step 2 there.
- **Python 3.10 or newer.** Ubuntu 22.04, Debian 12 and current macOS have it. Ubuntu 20.04 (Python 3.8) and Debian 11
  (Python 3.9) do not; step 2 shows how to run svqa on its own Python there, without root.
- **pipx.** Step 2 installs it.

## 1. Install FFmpeg

**macOS (Homebrew).** Homebrew's FFmpeg has libx264 and every filter svqa uses. If you don't have Homebrew yet, get it
from <https://brew.sh> first.

```sh
brew install ffmpeg
```

**Debian and Ubuntu.**

```sh
sudo apt update && sudo apt install ffmpeg
```

Ubuntu 20.04 and Debian 11 ship FFmpeg versions older than 4.4. On those releases, use a static build (see below).

**Fedora.** Fedora's own `ffmpeg-free` package has no libx264, so `svqa fix` cannot re-encode video with it. Enable RPM
Fusion (<https://rpmfusion.org/Configuration>), then swap the package:

```sh
sudo dnf swap ffmpeg-free ffmpeg --allowerasing
```

**Any Linux, without root or on an older release: a static build.** Download one of the Linux static builds linked
from <https://ffmpeg.org/download.html> (a `.tar.xz` file), unpack it into `~/opt/ffmpeg`, and tell svqa where it is:

```sh
mkdir -p ~/opt/ffmpeg
tar -xf ffmpeg-*-static.tar.xz -C ~/opt/ffmpeg --strip-components=1   # use the name of the file you downloaded
ls ~/opt/ffmpeg                                                        # ffmpeg and ffprobe are listed
export SVQA_FFMPEG="$HOME/opt/ffmpeg/ffmpeg"
export SVQA_FFPROBE="$HOME/opt/ffmpeg/ffprobe"
```

If `ls` shows a `bin` folder instead, the programs are in `~/opt/ffmpeg/bin/`: use that folder in the two `export`
lines. Add both `export` lines to `~/.bashrc` or `~/.zshrc` so they stay set. The same two variables also help when
another, older `ffmpeg` comes first on your `PATH`.

**Windows (untested).** Download a build linked from <https://ffmpeg.org/download.html>, then set `SVQA_FFMPEG` and
`SVQA_FFPROBE` to its `ffmpeg.exe` and `ffprobe.exe`.

Quick check (step 3 checks all of this and more). The first line makes it test your static build when you set
`SVQA_FFMPEG`, and the `ffmpeg` on your `PATH` otherwise:

```sh
FF="${SVQA_FFMPEG:-ffmpeg}"
"$FF" -hide_banner -version | head -n 1                                 # version 4.4 or newer
"$FF" -hide_banner -encoders | awk '$2 == "libx264"'                    # one line: libx264 is there
"$FF" -hide_banner -filters | grep -E ' (loudnorm|ebur128|highpass) '   # three lines
```

## 2. Install svqa

svqa is not on PyPI yet, so install it from GitHub with pipx:

```sh
pipx install git+https://github.com/deegitech/social-video-qa
svqa --version
```

For scripts and CI, pin a release so that updates happen only when you choose:

```sh
pipx install "git+https://github.com/deegitech/social-video-qa@v0.1.0"
```

**No git on the machine?** Install the release archive instead; it needs no git:

```sh
pipx install https://github.com/deegitech/social-video-qa/archive/refs/tags/v0.1.0.tar.gz
```

**No pipx yet?**

```sh
brew install pipx && pipx ensurepath          # macOS
sudo apt install pipx && pipx ensurepath      # Debian / Ubuntu
```

Then open a new terminal, so that `svqa` is on your `PATH`.

**Prefer a virtual environment?** On Debian and Ubuntu, run `sudo apt install python3-venv` first.

```sh
python3 -m venv ~/.venvs/svqa
~/.venvs/svqa/bin/pip install "social-video-qa[yaml] @ git+https://github.com/deegitech/social-video-qa"
source ~/.venvs/svqa/bin/activate             # puts svqa on PATH in this terminal
svqa --version
```

The later steps type `svqa`, so run the `source` line again in every new terminal. Or link the command once into a
folder on your `PATH` (here `~/.local/bin`, if it is on your `PATH`):
`mkdir -p ~/.local/bin && ln -sf ~/.venvs/svqa/bin/svqa ~/.local/bin/svqa`.

**Python older than 3.10** (Ubuntu 20.04, Debian 11, no root). uv can give svqa a Python of its own and leaves the
system Python alone. Install uv (it goes into `~/.local/bin`; see <https://docs.astral.sh/uv/> if the command has
changed), open a new terminal, then install svqa:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --python 3.12 git+https://github.com/deegitech/social-video-qa
```

With root, you can instead install a newer Python (the deadsnakes PPA on Ubuntu, or pyenv) and run
`pipx install --python python3.12 git+https://github.com/deegitech/social-video-qa`. The error you get without either is
in [troubleshooting.md](troubleshooting.md#installing-svqa) (`requires a different Python`).

**Optional profile formats.** Built-in and JSON profiles always work. YAML and TOML profiles need an extra package:

```sh
pipx inject social-video-qa PyYAML    # YAML profiles
pipx inject social-video-qa tomli     # TOML profiles, only on Python 3.10 (3.11+ reads TOML itself)
```

In a virtual environment, use `pip install PyYAML` (or `tomli`) there instead; with uv, add `--with PyYAML` to the
`uv tool install` command. `svqa doctor` prints the command that fits your install.

A plain `pip install` into the system Python stops with `error: externally-managed-environment` on Homebrew Python and
on recent Debian and Ubuntu. That is expected. Use pipx or a virtual environment, not `--break-system-packages`.

## 3. Verify everything with `svqa doctor`

```sh
svqa doctor
```

`svqa doctor` runs read-only checks in a fixed order and prints one line per item:

- `✓` means ready;
- `✗` means a problem, and the exact fix is printed under it;
- `!` means it works but is worth a look;
- `-` means optional and not used on this machine.

It finds the binaries, reads their versions, checks that each one is the program it should be, and checks the
demuxers, filters and encoders svqa uses and the lavfi input. It then runs a pipeline test: half a second of FFmpeg's built-in test picture and tone goes through libx264,
AAC, `loudnorm` and `ebur128` into FFmpeg's null output. Last, it validates the built-in profiles and any profile
folders you use. Nothing is written and nothing goes over the network. The exit code is 0 when everything is ready and
1 otherwise, so `svqa doctor || exit 1` works in scripts.

A ready machine (macOS with Homebrew):

```console
$ svqa doctor
svqa 0.1.0 doctor: what svqa check and svqa fix need (read-only, offline)

  ✓ python             3.12.4 (3.10 or newer needed)
  ✓ ffmpeg             /opt/homebrew/bin/ffmpeg (from PATH)
  ✓ ffprobe            /opt/homebrew/bin/ffprobe (from PATH)
  ✓ ffmpeg version     8.1.2 (4.4 or newer needed)
  ✓ ffprobe version    8.1.2 (same build as ffmpeg)
  ✓ demuxers           mov, matroska, avi, mpegts
  ✓ check filters      ebur128, highpass, blackdetect, freezedetect
  ✓ fix filters        loudnorm, aresample, afade, anullsrc, scale, pad, crop, split, boxblur, overlay, fps, format, setsar, bwdif
  ✓ encoders           libx264, aac
  ✓ lavfi input        present (svqa fix adds silent audio tracks with it)
  ✓ pipeline test      0.5 s of test video and audio through libx264, AAC, loudnorm, ebur128
  ✓ built-in profiles  7 load and validate
  - profile folders    none set (optional: --profile-dir DIR or SVQA_PROFILE_PATH)
  - profile formats    JSON, TOML; optional: YAML needs PyYAML: pipx inject social-video-qa PyYAML

ready: svqa check and svqa fix can use every feature.
next: svqa check clip.mp4   (first steps: https://github.com/deegitech/social-video-qa/blob/main/docs/setup.md)
```

A Linux machine whose FFmpeg has no libx264:

```console
$ svqa doctor
...
  ✓ fix filters        loudnorm, aresample, afade, anullsrc, scale, pad, crop, split, boxblur, overlay, fps, format, setsar, bwdif
  ✗ encoders           missing: libx264 (needed to re-encode video)
                       fix: install a full FFmpeg build:
                            Debian/Ubuntu: sudo apt install ffmpeg   (Ubuntu 20.04 and Debian 11 ship versions older than 4.4)
                            Fedora: the ffmpeg package from RPM Fusion (Fedora's own ffmpeg-free has no libx264)
                            any system: a static build linked from https://ffmpeg.org/download.html
                            if another ffmpeg comes first on PATH, point SVQA_FFMPEG and SVQA_FFPROBE at the new one
  ✓ lavfi input        present (svqa fix adds silent audio tracks with it)
  - pipeline test      skipped until the FFmpeg items above pass
...
not ready: 1 problem. Apply the fix under each ✗, then run svqa doctor again.
more help: https://github.com/deegitech/social-video-qa/blob/main/docs/troubleshooting.md
```

A variable that points at the wrong program (here `SVQA_FFPROBE` at `ffmpeg`):

```console
$ svqa doctor
...
  ✓ ffprobe            /opt/homebrew/bin/ffmpeg (from SVQA_FFPROBE)
  ✓ ffmpeg version     8.1.2 (4.4 or newer needed)
  ✗ ffprobe version    SVQA_FFPROBE points at ffmpeg (/opt/homebrew/bin/ffmpeg), not ffprobe
                       fix: export SVQA_FFPROBE=/opt/homebrew/bin/ffprobe
  - FFmpeg features    skipped until the ffmpeg and ffprobe items above pass
...
```

Apply the fix and run `svqa doctor` again until it says `ready`. With `--profile-dir DIR` it also validates the profile
files in `DIR`; folders listed in `SVQA_PROFILE_PATH` are always checked. doctor prints only paths and versions, never
the contents of your environment.

## 4. Your first check

No footage at hand? FFmpeg can make a 12-second test clip:

```sh
ffmpeg -f lavfi -i testsrc2=size=1080x1920:rate=30:duration=12 \
       -f lavfi -i sine=frequency=440:sample_rate=48000:duration=12 \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest clip.mp4
```

Without `-p`, svqa checks every built-in profile and prints one line each:

```console
$ svqa check clip.mp4
clip.mp4
  ✗ app-store-preview-ipad       FAIL (3 errors, 3 warnings): duration, bitrate.total, video.size, audio.channels, audio.bitrate, audio.loudness
  ✗ app-store-preview-iphone     FAIL (3 errors, 3 warnings): duration, bitrate.total, video.size, audio.channels, audio.bitrate, audio.loudness
  ✗ instagram-reels-api          FAIL (2 errors, 2 warnings): container.faststart, container.edit_list, video.b_frames, audio.loudness
  ✗ instagram-story-api          FAIL (2 errors, 2 warnings): container.faststart, container.edit_list, video.b_frames, audio.loudness
  ! meta-ads-instagram           PASS with warnings (4 warnings): container.faststart, audio.channels, audio.bitrate, audio.loudness
  ! tiktok                       PASS with warnings (1 warning): audio.loudness
  ! youtube-shorts               PASS with warnings (3 warnings, 2 notes): container.faststart, container.edit_list, audio.loudness

1 file x 7 profiles: 3 passed (3 with warnings), 4 failed · 10 errors, 18 warnings
```

With `-p` you get the detailed report for one platform:

```console
$ svqa check clip.mp4 -p instagram-reels-api
clip.mp4 · instagram-reels-api · FAIL (2 errors, 2 warnings)
  video h264 High@4.0 · 1080x1920 · 30 fps CFR · 6.6 Mbps · yuv420p · B-frames
  audio aac LC · 48 kHz · 1 ch · 69 kbps · -21.8 LUFS · TP -13.8 dBTP · hiss -70
  file  mp4 · 12.00 s · 10.1 MB · 6.7 Mbps · moov last · 2 edit list(s) · 9:16
  ✗ container.faststart  moov box is after mdat: the whole file must be read before playback or processing can start
  ✗ container.edit_list  edit list (elst) in 2 track(s): video, audio
  ! video.b_frames       B-frames present (from packet timestamps, x264 bframes=3)
                         note: Not in Meta's spec. Without B-frames an MP4 needs no edit list to start at time zero.
  ! audio.loudness       integrated loudness -21.8 LUFS: too quiet by 7.8 LU (target -14 +/- 1 LUFS)
                         note: Common streaming target; Meta publishes no loudness requirement.
  fix: svqa fix clip.mp4 -p instagram-reels-api

1 file x 1 profile: 0 passed (0 with warnings), 1 failed · 2 errors, 2 warnings
```

How to read it:

- `✗` is an error: the platform's spec says no, and the run exits with 1.
- `!` is a warning: the upload probably works, but something will look or sound worse. It passes unless you add
  `--strict`.
- `note:` says where a rule comes from and how sure it is. `observed` rules come from practice, not from the
  platform's documentation.
- `fix:` is the command that plans the repair.

[checks.md](checks.md) explains every check, and [profiles.md](profiles.md) lists every rule with its source.

## 5. Your first fix: dry run first

`svqa fix` writes nothing until you add `--apply`. Look at the plan first:

```console
$ svqa fix clip.mp4 -p instagram-reels-api
clip.mp4 -> clip.instagram-reels-api.mp4 · instagram-reels-api · PLAN (full)
  fails now: container.faststart (error), container.edit_list (error), video.b_frames (warn), audio.loudness (warn)
  video: re-encode with libx264 high, CRF 20, max 8000 kbps, preset medium, no B-frames, closed GOP every 60 frames, 1080x1920 (size kept), 30 fps CFR
  audio: re-encode AAC 128 kbps, 48000 Hz, 2 ch, two-pass loudnorm to -14 LUFS / -1.5 dBTP
  container: MP4, moov first, no edit list, metadata stripped
  dry run: nothing was written
  re-run with --apply to write it
```

Then write the fixed copy. svqa checks it again before it moves it into place:

```console
$ svqa fix clip.mp4 -p instagram-reels-api --apply
...
clip.mp4 -> clip.instagram-reels-api.mp4 · instagram-reels-api · WRITTEN (full)
  ...
  verified: PASS
  wrote clip.instagram-reels-api.mp4 (5.9 s)

$ svqa check clip.instagram-reels-api.mp4 -p instagram-reels-api
clip.instagram-reels-api.mp4 · instagram-reels-api · PASS
```

- Upload `clip.instagram-reels-api.mp4`, not the original. The input is never changed.
- `--show-commands` prints the exact `ffmpeg` commands.
- `-O DIR` writes the fixed copies into another folder.
- `.svqa-manifest.json` next to the output is the journal `fix` uses to skip up-to-date files. Add it to `.gitignore`.

[fix.md](fix.md) explains the modes, the encode settings and the safety steps.

## 6. Pick your profiles

| You upload | Profile | Worth knowing |
| --- | --- | --- |
| Reels through the Instagram Graph API | `instagram-reels-api` | moov first, no edit lists, 3 s to 15 min, 300 MB |
| Stories through the Instagram Graph API | `instagram-story-api` | 3 to 60 s, 100 MB; split longer stories |
| Meta ads in Instagram Reels and Stories placements | `meta-ads-instagram` | 15 s maximum (observed, warns; `--strict` enforces it) |
| App Store app previews | `app-store-preview-iphone`, `app-store-preview-ipad` | exact sizes, 15 to 30 s, up to 30 fps, an audio track |
| YouTube Shorts | `youtube-shorts` | square or vertical, up to 3 min |
| TikTok | `tiktok` | 23 to 60 fps, up to 10 min (3 min warns) |

`svqa profiles show ID` prints a profile with its sources and the dates they were checked.

**House rules.** Write a profile that extends a built-in one and changes only what differs
([writing-profiles.md](writing-profiles.md)). Keep your profiles in one folder and tell svqa where it is. This example
creates the folder and a first profile that also requires exactly 30 fps:

```sh
mkdir -p ~/video-profiles
cat > ~/video-profiles/brand-reels.json <<'EOF'
{
  "id": "brand-reels",
  "extends": "instagram-reels-api",
  "title": "Our Reels",
  "rules": {"video.fps": {"min": 30, "max": 30}}
}
EOF
export SVQA_PROFILE_PATH="$HOME/video-profiles"     # add this line to ~/.zshrc or ~/.bashrc
svqa profiles validate ~/video-profiles/brand-reels.json
svqa doctor                                          # checks the folder too
svqa check clip.mp4 -p brand-reels
```

[examples/profiles/](../examples/profiles/) has more to copy, in JSON, YAML and TOML.

## 7. Put it in front of your uploads

**A shell gate.** Upload only when the check passes:

```sh
svqa check exports/ -p instagram-reels-api --strict -q || { echo "not ready to upload"; exit 1; }
```

**CI.** [examples/github-actions.yml](../examples/github-actions.yml) is a complete workflow. It installs FFmpeg, runs
`svqa doctor` and checks every exported video, then publishes a JUnit report. Pin svqa to a release tag there.

**Servers.** svqa needs no network. If it processes files from strangers, run it in a container or sandbox with no
network access and keep FFmpeg patched. Use `--threads 1 --nice 19` on busy machines. Wrap `svqa fix` in `timeout`,
because only the analysis steps have a time limit:

```sh
timeout 1800 svqa fix incoming/ -p tiktok -O ready/ --apply
```

## Credentials: none

svqa needs no account, token, API key or password, and it reads no credentials: none from environment variables,
config files or the keychain. (The environment variables it reads are settings: `SVQA_FFMPEG`, `SVQA_FFPROBE`,
`SVQA_PROFILE_PATH`, `NO_COLOR`, and the usual `PATH` and `TERM`.) Don't pass secrets to it. Don't put them in profile files either, because profiles are plain data that
people share.

If the same pipeline then uploads with an API token (for example to the Instagram Graph API), set that token up as the
uploader's own guide describes. Two rules learned in production apply to any token:

- **macOS Keychain.** Store the token without it ever appearing on screen. The command reads the token from the
  clipboard, so the order matters. `my-uploader-token` is an example name; use the one your uploader expects.

  1. Type or paste only this line, and do not press Enter yet:

     ```sh
     security add-generic-password -U -a "$USER" -s my-uploader-token -w "$(pbpaste)"
     ```

  2. Copy the token from the console where you created it.
  3. Press Enter. The token goes from the clipboard straight into the Keychain.
  4. Clear the clipboard:

     ```sh
     pbcopy </dev/null
     ```

  Never paste the command and anything else at once. If the command runs while the clipboard still holds the command
  text, that text is stored as the token, and `-U` replaces a good token with it. If you leave `-w` without a value,
  macOS asks for the password interactively, and that prompt silently cuts the input at 128 characters. Meta tokens are
  about 200 characters, so always use the `"$(pbpaste)"` form. To check the length without showing the token, run
  `security find-generic-password -a "$USER" -s my-uploader-token -w | wc -c`. A Meta token gives about 200; 129
  (128 characters and a line end) means the prompt cut it.
- **Servers.** Use a file with mode `0600` outside any repository, or an AWS SSM Parameter Store `SecureString` read
  into memory or tmpfs at boot. Never echo it into CI logs.

## Keeping it current

Nothing in svqa expires: there are no tokens, licences or trials to renew. Three things do change:

- **Platform specs.** Every rule shows the date its source was checked (`svqa profiles show ID`). When a release
  updates a spec, update svqa:
  `pipx install --force "git+https://github.com/deegitech/social-video-qa@vX.Y.Z"`. A new svqa version or a changed
  profile changes the fix recipe, so the next `svqa fix --apply` re-encodes your outputs once instead of reporting them
  up to date.
- **FFmpeg.** Run `svqa doctor` after every FFmpeg upgrade. A partial Homebrew upgrade can leave FFmpeg unable to load
  its libraries; `brew reinstall ffmpeg` fixes that. If you check files from strangers, keep FFmpeg patched.
- **Your habits.** Make `svqa doctor` the first step of every CI run (it takes well under a second). A monthly
  calendar reminder to read [CHANGELOG.md](../CHANGELOG.md) for spec updates is enough.

## Common mistakes

> **Avoid these**
>
> 1. **Expecting a file from `svqa fix` without `--apply`.** Without it, fix only prints the plan.
> 2. **Uploading the original** instead of `<name>.<profile>.mp4`.
> 3. **Checking against the wrong profile.** Reels, Stories and ads have different limits. Stories stop at 60 s, and
>    Instagram ad placements accepted only videos up to 15 s: observed (Oct 2026), not documented.
> 4. **An FFmpeg without libx264** (minimal builds, Fedora's `ffmpeg-free`). `check` works, but `fix` cannot re-encode
>    video. `svqa doctor` shows it, and the `svqa fix` plan says so too.
> 5. **Two FFmpeg installs, or a variable on the wrong program**: an old `ffmpeg` earlier on `PATH`, `ffmpeg` and
>    `ffprobe` from different installs, or `SVQA_FFPROBE` set to the `ffmpeg` binary. `svqa doctor` prints the paths it
>    uses and names a variable that points at the wrong program. Point `SVQA_FFMPEG` and `SVQA_FFPROBE` at one install.
> 6. **`pip install` into the system Python** (`externally-managed-environment`). Use pipx or a virtual environment.
> 7. **`svqa: command not found` right after installing.** pipx: run `pipx ensurepath` and open a new terminal. Virtual
>    environment: run `source ~/.venvs/svqa/bin/activate` in each new terminal.
> 8. **`--fast` in CI.** It skips the frame-timing, loudness and opening-frame analyses, so variable frame rate, loud
>    audio and black openings go unnoticed.
> 9. **iPhone HDR footage.** `fix` converts HDR to 8-bit without tone mapping. Export SDR from your editor first.
> 10. **Reading a pass as a guarantee.** Platforms change their specs without notice, and `observed` rules come from
>     practice. A pass makes a rejection unlikely; it does not promise acceptance.
