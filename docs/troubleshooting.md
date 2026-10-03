# Troubleshooting

Find the message you see, or the closest symptom, and apply the fix. svqa prints the same one-line fix as `hint:`
under most of these errors (in JSON output, as the `hint` field), and `svqa doctor` checks the whole installation for
you. New to svqa? Start with [setup.md](setup.md).

Package and menu names change over time, so the ones named here may differ on your system. Platform behaviour that
the platforms do not document is marked **observed**, with the month it was seen, for example **observed (Oct 2026)**.

- [Start here: `svqa doctor`](#start-here-svqa-doctor)
- [Installing svqa](#installing-svqa)
- [FFmpeg](#ffmpeg)
- [Reading videos (`svqa check`, `svqa probe`)](#reading-videos-svqa-check-svqa-probe)
- [Fixing videos (`svqa fix`)](#fixing-videos-svqa-fix)
- [Profiles](#profiles)
- [The platform refused a file that svqa passed](#the-platform-refused-a-file-that-svqa-passed)
- [Exit codes](#exit-codes)

## Start here: `svqa doctor`

```sh
svqa doctor                         # add --profile-dir DIR to validate your own profile folder too
```

It prints `✓` or `✗` for each item, the exact fix under every `✗`, and exits 0 when everything is ready (1 if not).
It is read-only and offline: the pipeline test encodes half a second of FFmpeg's built-in test picture and tone into
FFmpeg's null output. The FFmpeg and profile rows below are the problems it can report.

## Installing svqa

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| `error: externally-managed-environment` | `pip` refuses to install into the system Python (Homebrew Python, recent Debian and Ubuntu). | Install with pipx: `pipx install git+https://github.com/deegitech/social-video-qa`, or use a virtual environment. Don't use `--break-system-packages`. |
| `ERROR: Cannot find command 'git' - do you have 'git' installed and in your PATH?` (pip), or pipx's `Cannot determine package name from spec 'git+https://...'` | The install from GitHub clones the repository with git, and this machine has no git. Common on slim containers and on Macs without the Command Line Tools. | Install git (macOS: `xcode-select --install`; Debian/Ubuntu: `sudo apt install git`; Fedora: `sudo dnf install git`). Or install the release archive, which needs no git: `pipx install https://github.com/deegitech/social-video-qa/archive/refs/tags/v0.1.0.tar.gz`. |
| `pipx: command not found` | pipx is not installed. | macOS: `brew install pipx && pipx ensurepath`. Debian/Ubuntu: `sudo apt install pipx && pipx ensurepath`. Open a new terminal. |
| `svqa: command not found` right after installing | pipx's folder for commands is not on your `PATH` yet, or the virtual environment is not active in this terminal. | pipx: `pipx ensurepath`, then open a new terminal. Virtual environment: `source ~/.venvs/svqa/bin/activate` (see [setup.md](setup.md#2-install-svqa)). |
| `ERROR: Package 'social-video-qa' requires a different Python: 3.9.6 not in '>=3.10'` | Your Python is older than 3.10 (Ubuntu 20.04 has 3.8, Debian 11 has 3.9). | Without root: install uv, then `uv tool install --python 3.12 git+https://github.com/deegitech/social-video-qa` (uv brings its own Python). With a newer Python installed: `pipx install --python python3.12 git+https://github.com/deegitech/social-video-qa` (use the version you installed). |
| `python3 -m venv` fails: `ensurepip is not available` | Debian and Ubuntu ship the venv module separately. | `sudo apt install python3-venv`, then create the environment again. |
| `YAML profiles need PyYAML (pip install 'social-video-qa[yaml]')` | YAML support is an optional extra. | `pipx inject social-video-qa PyYAML` (pipx), `uv tool install --python 3.12 --with PyYAML git+https://github.com/deegitech/social-video-qa` (uv, with the Python you installed it with), or `pip install PyYAML` in your virtual environment. The `hint:` line and `svqa doctor` print the one that fits your install. Or write the profile in JSON. |
| `TOML profiles need Python 3.11+, or pip install 'social-video-qa[toml]' on Python 3.10` | Python 3.10 cannot read TOML by itself. | Add `tomli` the same way as PyYAML above (for pipx: `pipx inject social-video-qa tomli`), or use Python 3.11+. |
| `profile formats: ... optional: YAML needs PyYAML` (doctor, `-`) | Only information: JSON profiles work, YAML would need PyYAML. | Nothing to do unless you write YAML profiles. |

## FFmpeg

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| `ffmpeg not found` / `ffprobe not found` (exit code 3) | The binary is not on your `PATH`, and `SVQA_FFMPEG` / `SVQA_FFPROBE` are not set. | macOS: `brew install ffmpeg`. Debian/Ubuntu: `sudo apt install ffmpeg`. Fedora: `ffmpeg` from RPM Fusion. Anywhere: a static build from <https://ffmpeg.org/download.html> plus `export SVQA_FFMPEG=/path/to/ffmpeg SVQA_FFPROBE=/path/to/ffprobe`. Then `svqa doctor`. |
| `SVQA_FFMPEG is set to ..., which is not an executable file` (doctor) | The variable points at a file that does not exist or cannot run. | Point it at the binary (`export SVQA_FFMPEG=/path/to/ffmpeg`), or `unset SVQA_FFMPEG` to use the one on `PATH`. The same goes for `SVQA_FFPROBE`. |
| `ffmpeg version: 4.2.7 is older than 4.4` (doctor) | svqa needs FFmpeg 4.4 or newer. Ubuntu 20.04 ships 4.2 and Debian 11 ships 4.3. | Upgrade FFmpeg (`brew upgrade ffmpeg`), or use a static build from <https://ffmpeg.org/download.html> with `SVQA_FFMPEG` and `SVQA_FFPROBE`. |
| `ffprobe version: SVQA_FFPROBE points at ffmpeg (...), not ffprobe` (doctor), or `ffmpeg version: SVQA_FFMPEG points at ffprobe (...)` | The variable names the wrong FFmpeg program. Each binary's `-version` output says which program it is. | Apply the `export` line doctor prints: it names the right binary in the same folder, for example `export SVQA_FFPROBE=/opt/homebrew/bin/ffprobe`. |
| `the ffprobe on PATH (...) is really ffmpeg` or `ffmpeg and ffprobe are the same file (...)` (doctor) | A link or wrapper named `ffprobe` runs `ffmpeg`, or both variables name one file. | Point `SVQA_FFMPEG` and `SVQA_FFPROBE` at the `ffmpeg` and `ffprobe` of one install, or reinstall FFmpeg. |
| `ffprobe version: 6.1.1, but ffmpeg is 8.1.2: two different installs` (doctor, `!`) | `ffmpeg` and `ffprobe` come from different installs, for example one from Homebrew and one from a conda environment. Usually harmless, but results can differ. | Point `SVQA_FFMPEG` and `SVQA_FFPROBE` at binaries in the same folder. |
| `dyld[...]: Library not loaded: ...` or `error while loading shared libraries: ...` | FFmpeg cannot load its shared libraries, often after a partial upgrade. | macOS: `brew reinstall ffmpeg`. Debian/Ubuntu: `sudo apt install --reinstall ffmpeg`. Then `svqa doctor`. |
| `ffmpeg could not be started: ...` or `nice: /path/to/ffmpeg: Permission denied` | The system cannot run the binary: it was removed or has no execute permission. | Reinstall FFmpeg, or fix `SVQA_FFMPEG` / `SVQA_FFPROBE`. Then `svqa doctor`. |
| `Bad CPU type in executable` (macOS) or `Exec format error` (Linux) | The binary was built for another processor, for example an Intel-only FFmpeg on Apple silicon without Rosetta. | Install a native build (`brew install ffmpeg` on macOS, or a static build for your processor), or point `SVQA_FFMPEG` / `SVQA_FFPROBE` at one. Then `svqa doctor`. |
| `Unknown encoder 'libx264'` or `Encoder not found` | This FFmpeg build has no libx264. Minimal builds and Fedora's `ffmpeg-free` leave it out. `svqa check` still works, but `svqa fix` cannot re-encode video. | Install a full build: `brew install ffmpeg` (macOS), `sudo apt install ffmpeg` (Debian/Ubuntu), `sudo dnf swap ffmpeg-free ffmpeg --allowerasing` after enabling RPM Fusion (Fedora), or a static build. If another `ffmpeg` comes first on `PATH`, set `SVQA_FFMPEG` and `SVQA_FFPROBE`. |
| `No such filter: 'loudnorm'; Error opening output files: Filter not found` (or `'ebur128'`, `'highpass'`, `'blackdetect'`, ...) | This FFmpeg build lacks a filter svqa uses. | Install a full FFmpeg 4.4+ build as above. Until then, `svqa check --fast` skips the analyses that need filters, and `svqa fix --no-loudnorm` leaves loudness alone. |
| `Unknown input format: 'lavfi'` | The build has no lavfi input (libavdevice). `svqa fix` uses it only to add a silent audio track (`audio.present`, App Store previews). | Install a full FFmpeg build as above. |
| `Unrecognized option 'show_format'` | `SVQA_FFPROBE` (or the `ffprobe` on `PATH`) is really `ffmpeg`. | Point `SVQA_FFPROBE` at the `ffprobe` binary. `svqa doctor` names the right one. |
| `Failed to set value '-hide_banner' for option 'nostdin': Option not found` | `SVQA_FFMPEG` (or the `ffmpeg` on `PATH`) is really `ffprobe`. | Point `SVQA_FFMPEG` at the `ffmpeg` binary. `svqa doctor` names the right one. |
| `Unrecognized option '...'` (any other option) | The FFmpeg is too old for an option svqa uses. | Install FFmpeg 4.4 or newer. `svqa doctor` shows the version in use. |
| `pipeline test: a 0.5 s test encode failed: ...` (doctor) | The parts are listed, but the encode chain does not work. The reason after the colon says why. | Apply the fix printed under it. If none is printed, update or reinstall FFmpeg, run `svqa doctor` again, and if it still fails, open an issue with the output. |

## Reading videos (`svqa check`, `svqa probe`)

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| `no video files found` (exit code 2) | The folders you named contain no files with a video extension. | svqa looks for `.mp4 .mov .m4v .webm .mkv .avi .ts .3gp` (any letter case) and skips hidden files and folders. Name files directly to check other extensions. |
| `unsupported container (mp3): svqa only opens MP4/MOV/M4V/3GP, Matroska/WebM, AVI and MPEG-TS files` | The file's real format is not on svqa's input whitelist. | If it is a video in another container, convert it first: export as MP4, or `ffmpeg -i INPUT -c copy OUTPUT.mp4`. HLS playlists and `ffconcat` scripts are refused on purpose (see the security model in the README). |
| `ffprobe could not read the file: moov atom not found; Invalid data found when processing input` | The MP4/MOV index (the `moov` box) is missing. The file is incomplete, because an export, recording or copy was cut off, or it is not really an MP4. | Export, download or copy it again. `svqa fix` cannot repair a file without its index. |
| `ffprobe could not read the file: Invalid data found when processing input` | FFprobe cannot parse the file: it is damaged, not a video, or has the wrong extension. | Check that it plays in a video player, then export it again. |
| `file is empty` | The file has 0 bytes. | The export or copy did not finish. Export it again. |
| `not a regular file` | You named a device, pipe or socket. | Give svqa video files or folders. |
| `cannot open: No such file or directory` | The path is wrong. | Check the path, and quote names that contain spaces. |
| `ffprobe could not read the file: Permission denied` (the file is not readable) or `cannot open: Permission denied` (a folder on the path cannot be opened) | You cannot read the file. | Check the permissions with `ls -l FILE` and `ls -ld FOLDER` and fix them, or copy the file somewhere you can read it. |
| `no video stream (audio-only file); every profile here is for video` | The file has sound but no picture. | Export the clip with its video track. |
| `loudness analysis failed: ffmpeg timed out after 600 s` (or `packets`, `hiss`, `start`) | One analysis step took longer than `--timeout`. Very long or very large files can. | Raise the limit (`--timeout 1800`), or skip the decoding analyses with `--fast`. |
| `loudness analysis failed: ebur128 analysis failed: No such filter: 'ebur128'; Error opening output files: Filter not found` | The FFmpeg build lacks the filter. The same failure shows under `audio.loudness`, `audio.true_peak`, `audio.hiss` and `audio.silence`, with one `hint:`. | See [FFmpeg](#ffmpeg). |
| A check shows `skip` with `not measured (fast mode)` | You used `--fast`. | Drop `--fast` to run the frame-timing, loudness, hiss and opening-frame analyses. |
| `ffprobe returned invalid JSON` | The `ffprobe` in use printed something other than its JSON. | Point `SVQA_FFPROBE` at the `ffprobe` of the same install as your `ffmpeg`, then `svqa doctor`. |

## Fixing videos (`svqa fix`)

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| `PLAN` and `re-run with --apply to write it`, but no file appears | `svqa fix` is a dry run by default. | Add `--apply`. |
| Plan note: `this FFmpeg has no libx264 encoder: --apply will fail (run svqa doctor)` | The plan re-encodes video, and this FFmpeg build cannot (minimal builds, Fedora's `ffmpeg-free`). | Install a full FFmpeg build (see [FFmpeg](#ffmpeg)), then plan again. |
| `UP TO DATE` when you expected a new encode | Same source, profile and settings as last time, so nothing changes. | Nothing to do. `--force` re-encodes anyway. |
| `cannot produce a passing file: duration ... is above the maximum ... (use --trim to cut it)` | The video is longer than the profile allows, and svqa does not cut without being asked. | Add `--trim` (it cuts 0.1 s under the maximum and fades the audio out), or edit the video. |
| `cannot produce a passing file: ...: ... (cannot be fixed automatically)` | Hiss, a black or frozen opening, silence, or a video that is too short. None of these can be repaired mechanically. | Fix it in your editor and export again. If it is intended, switch the check off with `--skip CHECK` or in your own profile. With `--strict`, warnings count too; drop `--strict` if a warning is acceptable. |
| `the video cannot fit the size/bitrate limits at a usable quality (cap ... kbps)` | The video is too long for the profile's file-size limit. | Shorten it or split it. If it is also over the maximum duration, `--trim` cuts it there. |
| `... video cannot be copied into MP4; use --mode full` | A forced `--mode remux` or `--mode audio` cannot carry this codec in MP4. | Use the default `--mode auto`. |
| `... needs re-encoding; --mode remux copies streams (use --mode auto)` | The forced mode cannot fix that check. | Use `--mode auto`. |
| `the video has B-frames and the profile forbids edit lists, so it must be re-encoded (use --mode auto)` | Dropping the edit list without re-encoding would shift the picture against the sound. | Use `--mode auto`. |
| `fix needs one profile, not 'all'` (exit code 2) | `fix` writes one copy per profile. | Name one profile with `-p`. |
| `--output needs exactly one input file; use --out-dir for several` | `-o FILE` works for a single input only. | Use `-O DIR` for several inputs. |
| `output would overwrite the input; choose another --output` | `-o` names the input file. | Choose another output name. |
| `... is also the output for ...; rename one of them, ...` | Two inputs would write the same file (`intro.mov` and `intro.mp4`, or the same name from two folders). | Rename one input, or fix it on its own with `-o FILE` or another `-O DIR`. |
| `... exists and was not written by this tool (or was modified); use --force to replace it` | svqa never overwrites a file it did not write. | Move the file away, choose another `-O DIR`, or add `--force`. |
| `... was written from another source (...); use --force to replace it, or choose another --out-dir` | The existing output came from a different input file. | Add `--force` if replacing it is intended, or choose another `-O DIR`. |
| `... is up to date, but its verification warned about ..., which --strict treats as failures` | The fixed copy has warnings that `--strict` turns into failures. | Fix the cause in the source (for example hiss), or run without `--strict`. |
| `another svqa fix run is writing to DIR (DIR/.svqa-manifest.lock exists)` | Two runs must not write one folder at once. | Wait for the other run. If none is running, delete the lock file. svqa clears a lock by itself when the process that took it no longer exists (macOS, Linux) or, on other systems, when it is older than six hours. |
| `.../.svqa-manifest.json is unreadable (...)` or `is not a social-video-qa manifest` | The journal in the output folder is damaged or belongs to something else. | Move it away. svqa starts a fresh journal, and existing outputs then need `--force` once. |
| `FAILED VERIFICATION` and `the re-encoded file still fails: ...` | The fixed copy was checked again and still breaks an error-level rule, so it was not published. | Re-run with `--keep-failed --show-commands` and inspect `*.failed.mp4`. If the profile's fix settings cannot reach the limit, open an issue with the `svqa probe` output. |
| `loudnorm pass 1 printed no measurements` | FFmpeg's `loudnorm` filter printed no JSON. | Update FFmpeg to 4.4 or newer, or skip normalisation with `--no-loudnorm`. |
| `No space left on device` | The output disk is full. | Free some space, or write to another disk with `-O DIR`. |
| `Permission denied` while writing | You cannot write to the output folder. | Choose a writable folder with `-O DIR`. |
| Plan note: `the source is HDR (arib-std-b67); it is re-encoded to 8-bit yuv420p without tone mapping` | iPhone footage is HLG HDR by default, and `fix` does not tone-map. | Export SDR from your editor, then run `svqa fix` on that file. |

## Profiles

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| `unknown profile 'x'. Built-in profiles: ...` (exit code 2) | No built-in profile and no file in your profile folders has that ID. | `svqa profiles` lists them. For your own profiles, add `--profile-dir DIR`, set `SVQA_PROFILE_PATH`, or pass a path such as `./my-profile.json`. |
| `profile file not found: ...` | The path does not exist. | On the command line a path is relative to the current folder; in `extends` it is relative to the folder of the profile that names it. |
| `profile 'x' (...) is invalid:` then `- rules.durration: unknown check` | A typo or a wrong value. Unknown keys are errors on purpose. | `svqa profiles validate FILE` lists every problem. [writing-profiles.md](writing-profiles.md) lists every key and parameter. |
| `rules.video.fps.maxx: unknown parameter (known: ...)` | The parameter name is wrong. | Use one of the names listed in the message. |
| `rules.audio.loudness: target_lufs is required` | Some checks need a parameter. | Add it (`audio.loudness` needs `target_lufs`, `audio.true_peak` needs `max_dbtp`, `video.keyint` needs `max_s`). |
| `profile inheritance loop: ./a.json -> ./b.json -> ./a.json` | Profiles extend each other in a circle. | Remove one of the `extends`. |
| `...: cannot parse: ...` | A JSON, YAML or TOML syntax error. | Fix the syntax at the line the message names. |
| `profile folder: DIR does not exist (from SVQA_PROFILE_PATH)` (doctor) | The variable or `--profile-dir` names a missing folder. | `mkdir -p DIR`, or correct the variable. |
| `profile folder: DIR: 2 of 3 invalid (...)` (doctor) | Files in your profile folder do not validate. | Apply the fix shown for the first one. The `see every problem:` line is the `svqa profiles validate` command for every invalid file. |

## The platform refused a file that svqa passed

svqa checks files before you upload them. It cannot see tokens, permissions or rate limits. Errors about those come
from the tool that uploads (for Instagram, see [ig-publish](https://github.com/deegitech/ig-publish)), not from the
file.

| Message or symptom | What it means | Fix |
| --- | --- | --- |
| Instagram Graph API error `2207052` or `9004`: the media could not be fetched | Meta's servers could not download the file from your `video_url`. That is not a file-format problem. Observed (Oct 2026): Meta's fetcher sometimes rejects valid URLs. | Publish with a resumable upload (Facebook Login on `graph.facebook.com`), which sends the file itself and needs no public URL. With Instagram Login (`graph.instagram.com`), every file needs a public `video_url`. |
| The Instagram API accepts the upload, then reports a generic "unsupported format" error | The container details Meta checks: `moov` after `mdat`, edit lists, B-frames, frame rate, audio format. | `svqa check FILE -p instagram-reels-api` (or `instagram-story-api`), then `svqa fix FILE -p ... --apply`. |
| A story is rejected for its length or size | The Stories limits are 3 to 60 s and 100 MB. | `svqa check FILE -p instagram-story-api`. Split longer stories into several. |
| The Marketing API reports `is_instagram_eligible=false` for an ad video | Observed (Oct 2026): videos longer than 15 s were not eligible for Instagram ad placements. The same video cut to 14.8 s became eligible, and the bitrate made no difference. Meta's Ads Guide lists longer durations, so this is not documented. | `svqa check FILE -p meta-ads-instagram --strict` flags it. `svqa fix FILE -p meta-ads-instagram --trim --apply` cuts the video to 14.9 s. |
| App Store Connect refuses an app preview | A size that is not on Apple's list, more than 30 s, more than 30 fps, or no audio track (observed (Sep 2026, developer reports): uploads without an audio track fail). | `svqa check FILE -p app-store-preview-iphone` (or `-ipad`). `svqa fix` adds a silent track, and `--trim` shortens the preview. |
| The video looks cropped or letterboxed in the feed | The aspect ratio differs from the placement's. | `video.aspect` flags it. `svqa fix --fit pad`, `crop` or `blur` reframes the video; for important videos, reframe by hand. |
| The audio drifts out of sync after the platform re-encodes the video | Variable frame rate. | `video.cfr` flags it. `svqa fix` writes a constant frame rate. |
| The cover or thumbnail is black | The video starts on black frames. | `content.black_start` flags it. Start the edit on a real frame. |
| The upload revealed where it was filmed | Phones store the location in the file's metadata. | `metadata.location` flags it. `svqa fix` strips metadata by default. |

## Exit codes

| Code | `svqa check`, `fix`, `probe` | `svqa doctor` |
| --- | --- | --- |
| 0 | Everything passed (warnings allowed unless `--strict`). | Ready. |
| 1 | A check or fix failed, or a file could not be read. | At least one `✗`. |
| 2 | Usage or profile error. | Usage error. |
| 3 | `ffmpeg` or `ffprobe` not found. | Not used: a missing binary is a `✗` (exit code 1). |
