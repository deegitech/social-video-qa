# Checks reference

Every check has an ID (`video.fps`, `audio.loudness`, ...). A profile turns a check on by listing its ID under `rules`;
the same ID appears in reports, in `--skip` / `--only`, and in JUnit test case names.

## What is measured, and how

All measurements are local and read-only. The tool runs FFprobe and FFmpeg with an argument list (never a shell) and opens
your file only as a `file:` URL, with a protocol and demuxer whitelist (see the Security model in the README).

| Analysis | Tool | Cost | Used by |
| --- | --- | --- | --- |
| Streams and format | `ffprobe -show_format -show_streams` | instant | most checks |
| Box layout | built-in MP4/MOV box reader (`moov`, `mdat`, `elst`, `tkhd`, `ftyp`) | instant | `container.*` |
| Encoder settings | x264/x265 settings string in the first 4 MB | instant | fallbacks for B-frames and GOP |
| Packets | `ffprobe -show_entries packet=pts,dts,duration,size,flags` on the video stream | demux only, no decoding | frame rate, CFR, B-frames, GOP, keyframe interval, peak bitrate |
| Loudness | `ffmpeg -af ebur128=peak=true` on the audio stream | audio decode | `audio.loudness`, `audio.true_peak`, `audio.silence` |
| Hiss index | `ffmpeg -af highpass=f=6000,highpass=f=6000,ebur128` | audio decode | `audio.hiss` |
| Opening frames | `blackdetect` + `freezedetect` on the first seconds (3 by default) | short video decode | `content.*` |

`--fast` skips the packet, loudness, hiss and opening-frame analyses. Checks that need them are then reported as
skipped, except four that fall back to cheaper evidence: `video.b_frames` and `video.fps` use the stream headers,
`video.bitrate` uses the bitrate the container reports, and `video.closed_gop` still fails on an x264/x265
`open_gop=1` setting.

### Frame timing (CFR) and frame rate

Packet timestamps are sorted into presentation order and the intervals between them are compared with the median
interval. An interval more than `tolerance_pct` (default 2%) away from the median counts as irregular, with at least
one timestamp tick of slack, because WebM stores milliseconds and a 30 fps stream alternates 33 and 34 ms. The first and
last intervals are ignored: when an MP4 is written without edit lists, muxers stretch the first frame to absorb the AAC
encoder's priming delay (about 21 ms at 48 kHz), and the last frame's duration is often a guess. The frame rate shown and
checked is 1 / mean interval over the same frames. That is more reliable than the container's average for short clips,
and it is the true average rate for variable-frame-rate material.

### B-frames and closed GOPs

A stream uses B-frames (picture reordering) when, in decode order, a packet's presentation time is earlier than one
already seen. A GOP is **open** when a picture decoded after a keyframe is presented before it (a "leading picture").
This is how open GOPs look for both H.264 (I-frames with recovery points) and HEVC (CRA with RASL pictures). The x264/x265
settings string (`open_gop=1`) is used as extra evidence. The detection assumes the muxer flags open-GOP I-frames as
keyframes, which FFmpeg does; if an encoder does not, only the settings string can reveal the open GOP.

### Edit lists and moov position

The box reader walks the top-level boxes without loading the media data, then reads only the `moov` box (up to 256 MB) to
find `elst` boxes, `tkhd` enabled flags and handler types. Any `elst` box counts as an edit list, including trivial ones,
because Meta's and YouTube's guidance does not distinguish them.

### Loudness and true peak

Integrated loudness follows EBU R128 / ITU-R BS.1770 (K-weighting, 400 ms blocks, absolute gate at -70 LUFS, relative gate
at -10 LU), as implemented by FFmpeg's `ebur128` filter. True peak is FFmpeg's oversampled true-peak measurement. A track
that never rises above the -70 LUFS gate is reported as silent; loudness and hiss checks skip it.

### The hiss index (an observed heuristic)

The hiss index is the integrated loudness of what is left after two cascaded 2-pole high-pass filters at 6 kHz (about
24 dB per octave). It estimates how much energy sits in the band where hiss, noise beds, harsh sibilance and noisy sound
effects live.

It is a heuristic, not a standard. The thresholds come from game-trailer mixes normalised to about -14 LUFS (measured in
October 2026), where clean audio measured roughly **-40 to -47 LUFS** and audibly hissy audio roughly **-26 to -30
LUFS**. Profiles warn above -35 LUFS. Bright material (cymbals, synth leads, speech with strong sibilance) raises the
index without being a problem, and quiet masters lower it. The JSON report also gives the index relative to the programme
loudness (`hiss_relative_lu`). Use `max_relative_lu` in your own profile if your audio is not normalised. `svqa fix`
cannot remove hiss; fix it in the mix.

### Opening frames

`blackdetect` (98% of pixels below 10% luma) and `freezedetect` (-60 dB noise tolerance) run on the first `window_s`
seconds. Touching segments are merged, because a keyframe re-quantises a still picture just enough to split one freeze
in two. A black opening is reported by `content.black_start` only, not also as a freeze.

### Location metadata

Phones store the recording location in container tags such as `com.apple.quicktime.location.ISO6709` or `location`. Any
tag whose name mentions location, GPS, ISO 6709 or `xyz` is reported. `svqa fix` strips container and stream metadata
by default (`--keep-metadata` keeps it).

## Results and exit codes

Each check reports `pass`, `fail` (with a level: error, warn or info), `skip` (not applicable or not measured) or
`error` (the measurement itself failed). In JSON, failed results also carry `fixable`, which says whether `svqa fix` can
repair them.

| Exit code | Meaning |
| --- | --- |
| 0 | Every file passed (warnings allowed unless `--strict`). For `fix`: every file was planned, written, up to date or needed nothing. |
| 1 | At least one error-level failure, an unreadable file, or a fix that was refused or failed verification. |
| 2 | Usage or profile error. |
| 3 | `ffmpeg` / `ffprobe` not found. A binary that is found but fails to run makes each file an error instead (exit code 1); `svqa doctor` shows why. |

`svqa doctor` uses only 0 (ready) and 1 (something is missing, including FFmpeg itself). Every error message, with its
meaning and fix, is in [troubleshooting.md](troubleshooting.md).

## All checks

<!-- Everything below is generated by scripts/gen_docs.py from the code. Do not edit it by hand. -->

| Check | Default severity | Fixable by `svqa fix` | Parameters |
| --- | --- | --- | --- |
| [`file.size`](#filesize) | error | yes, re-encodes the video | `min_mb` (number), `max_mb` (number) |
| [`container.format`](#containerformat) | error | yes, without re-encoding (remux) | `allowed` (list of strings), `recommended` (list of strings) |
| [`container.faststart`](#containerfaststart) | error | yes, without re-encoding (remux) | none |
| [`container.edit_list`](#containeredit_list) | error | yes, without re-encoding (remux) | none |
| [`container.streams`](#containerstreams) | warn | yes, without re-encoding (remux) | `max_video` (integer), `max_audio` (integer) |
| [`container.tracks_enabled`](#containertracks_enabled) | warn | yes, without re-encoding (remux) | none |
| [`duration`](#duration) | error | only too long, with `--trim` | `min_s` (number), `max_s` (number), `recommended_min_s` (number), `recommended_max_s` (number) |
| [`bitrate.total`](#bitratetotal) | error | yes, re-encodes the video | `min_kbps` (number), `max_kbps` (number), `recommended_min_kbps` (number), `recommended_max_kbps` (number) |
| [`video.codec`](#videocodec) | error | yes, re-encodes the video | `allowed` (list of strings), `recommended` (list of strings) |
| [`video.profile`](#videoprofile) | error | yes, re-encodes the video | `allowed` (mapping of codec name to list of strings) |
| [`video.level`](#videolevel) | error | yes, re-encodes the video | `max` (mapping of codec name to number) |
| [`video.pix_fmt`](#videopix_fmt) | error | yes, re-encodes the video | `allowed` (list of strings), `recommended` (list of strings) |
| [`video.chroma`](#videochroma) | error | yes, re-encodes the video | `allowed` (list of strings), `recommended` (list of strings) |
| [`video.progressive`](#videoprogressive) | error | yes, re-encodes the video | none |
| [`video.size`](#videosize) | error | yes, re-encodes the video | `allowed` (list of "WxH" strings), `recommended` (list of "WxH" strings) |
| [`video.resolution`](#videoresolution) | error | yes, re-encodes the video | `min_width` (integer), `max_width` (integer), `min_height` (integer), `max_height` (integer) |
| [`video.aspect`](#videoaspect) | error | yes, re-encodes the video | `min` (ratio (number or "W:H" string)), `max` (ratio (number or "W:H" string)), `recommended` (ratio or list of ratios), `tolerance` (number) |
| [`video.sar`](#videosar) | warn | yes, re-encodes the video | none |
| [`video.rotation`](#videorotation) | info | yes, re-encodes the video | none |
| [`video.fps`](#videofps) | error | yes, re-encodes the video | `min` (number), `max` (number), `recommended_min` (number), `recommended_max` (number) |
| [`video.cfr`](#videocfr) | warn | yes, re-encodes the video | `tolerance_pct` (number), `max_irregular_pct` (number) |
| [`video.bitrate`](#videobitrate) | error | yes, re-encodes the video | `min_kbps` (number), `max_kbps` (number), `recommended_min_kbps` (number), `recommended_max_kbps` (number) |
| [`video.b_frames`](#videob_frames) | warn | yes, re-encodes the video | none |
| [`video.closed_gop`](#videoclosed_gop) | error | yes, re-encodes the video | none |
| [`video.keyint`](#videokeyint) | warn | yes, re-encodes the video | `max_s` (number) |
| [`audio.present`](#audiopresent) | error | yes, re-encodes the audio only | none |
| [`audio.codec`](#audiocodec) | error | yes, re-encodes the audio only | `allowed` (list of strings), `recommended` (list of strings) |
| [`audio.profile`](#audioprofile) | warn | yes, re-encodes the audio only | `allowed` (mapping of codec name to list of strings) |
| [`audio.sample_rate`](#audiosample_rate) | error | yes, re-encodes the audio only | `allowed` (list of integers), `recommended` (list of integers), `min_hz` (integer), `max_hz` (integer) |
| [`audio.channels`](#audiochannels) | error | yes, re-encodes the audio only | `allowed` (list of integers), `recommended` (list of integers) |
| [`audio.bitrate`](#audiobitrate) | warn | yes, re-encodes the audio only | `min_kbps` (number), `max_kbps` (number), `recommended_min_kbps` (number), `recommended_max_kbps` (number) |
| [`audio.loudness`](#audioloudness) | warn | yes, re-encodes the audio only | `target_lufs` (number), `tolerance_lu` (number) |
| [`audio.true_peak`](#audiotrue_peak) | warn | yes, re-encodes the audio only | `max_dbtp` (number) |
| [`audio.hiss`](#audiohiss) | warn | no | `max_lufs` (number), `max_relative_lu` (number) |
| [`audio.silence`](#audiosilence) | info | no | none |
| [`content.black_start`](#contentblack_start) | warn | no | `max_s` (number), `window_s` (number) |
| [`content.frozen_start`](#contentfrozen_start) | warn | no | `max_s` (number), `window_s` (number) |
| [`metadata.location`](#metadatalocation) | warn | yes, without re-encoding (remux) | none |

### `file.size`

**File size.** Size on disk in decimal megabytes (1 MB = 1,000,000 bytes, the stricter reading).

How to fix: Lower the bitrate or shorten the video. `svqa fix` caps the bitrate so the file fits.

### `container.format`

**Container format.** Container family: mp4, mov, m4v, 3gp (from the ftyp brand), webm, mkv, avi, ts.

How to fix: Remux into MP4: `svqa fix` does this without re-encoding when the codecs allow it.

### `container.faststart`

**moov before mdat (fast start).** Top-level box order: the moov (index) box must come before the first mdat (media data).

How to fix: Remux with `-movflags +faststart`; `svqa fix` does this without re-encoding.

### `container.edit_list`

**No edit lists.** No track may contain an edit list (elst box).

How to fix: Edit lists usually come from B-frames or AAC priming. `svqa fix` writes files without them.

### `container.streams`

**Stream count.** Number of video streams (cover-art pictures excluded) and audio streams.

How to fix: Keep one video and one audio stream; `svqa fix` drops the rest.

### `container.tracks_enabled`

**All tracks enabled.** Every track's tkhd 'enabled' flag is set (Apple: 'All tracks should be enabled').

How to fix: Remux the file; `svqa fix` writes enabled tracks only.

### `duration`

**Duration.** Container duration in seconds.

How to fix: Edit the video. `svqa fix --trim` cuts videos that are too long (with a short audio fade).

### `bitrate.total`

**Overall bitrate.** Average bitrate of the whole file (video + audio + overhead) in kbps.

How to fix: Re-encode at a different bitrate; `svqa fix` uses the profile's encode settings.

### `video.codec`

**Video codec.** FFprobe codec name of the first video stream (h264, hevc, vp9, av1, prores, ...).

How to fix: Re-encode to H.264 with `svqa fix`.

### `video.profile`

**Video codec profile.** Codec profile per codec, as FFprobe names it (High, Main, Constrained Baseline, Main 10, ...).

How to fix: Re-encode with the right profile; `svqa fix` encodes H.264 High.

### `video.level`

**Video codec level.** Codec level per codec as a decimal (H.264 level 40 -> 4.0, HEVC 120 -> 4.0).

How to fix: Re-encode with a lower level (smaller frame size, frame rate or bitrate); `svqa fix` sets it.

### `video.pix_fmt`

**Pixel format.** Exact FFmpeg pixel format (yuv420p, yuvj420p, yuv420p10le, ...).

How to fix: Re-encode to yuv420p; `svqa fix` does.

### `video.chroma`

**Chroma subsampling.** Chroma subsampling derived from the pixel format: 4:2:0, 4:2:2, 4:4:4, 4:0:0.

How to fix: Re-encode to 4:2:0 (yuv420p); `svqa fix` does.

### `video.progressive`

**Progressive scan.** Field order must be progressive (or unsignalled); tt/bb/tb/bt mean interlaced.

How to fix: De-interlace and re-encode; `svqa fix` does (bwdif).

### `video.size`

**Frame size.** Displayed frame size (after rotation metadata) against exact WxH lists.

How to fix: Scale/pad/crop to an accepted size; `svqa fix --fit pad|crop|blur` does.

### `video.resolution`

**Resolution limits.** Displayed width and height against minimum/maximum pixel limits.

How to fix: Scale the video; `svqa fix` does.

### `video.aspect`

**Aspect ratio.** Displayed width/height (rotation and pixel aspect applied). min/max are hard limits; 'recommended' (default tolerance 1%) only warns.

How to fix: Pad, crop or blur-fill to the recommended ratio; `svqa fix --fit` does.

### `video.sar`

**Square pixels.** Sample (pixel) aspect ratio must be 1:1 or unset.

How to fix: Re-encode with square pixels; `svqa fix` resamples the picture to square pixels.

### `video.rotation`

**No rotation metadata.** Rotation side data (display matrix) that asks players to rotate the stored picture.

How to fix: Bake the rotation into the pixels; `svqa fix` does (FFmpeg auto-rotates while re-encoding).

### `video.fps`

**Frame rate.** Frame rate as 1 / mean packet interval, first and last excluded (falls back to avg_frame_rate in fast mode).

Needs analysis: packets (skipped with `--fast`).

How to fix: Convert the frame rate; `svqa fix` picks a cadence-friendly rate inside the allowed range.

### `video.cfr`

**Constant frame rate.** Every frame interval (except the first and last) within tolerance_pct (default 2%) of the median.

Needs analysis: packets (skipped with `--fast`).

How to fix: Convert to constant frame rate; `svqa fix` does (fps filter).

### `video.bitrate`

**Video bitrate.** Average video stream bitrate in kbps (container value, or packet sizes over duration).

Needs analysis: packets (skipped with `--fast`).

How to fix: Re-encode with a bitrate cap; `svqa fix` does.

### `video.b_frames`

**No B-frames.** Picture reordering in packet timestamps (pts order differs from decode order), plus has_b_frames and the x264/x265 settings string as fallbacks.

Needs analysis: packets (skipped with `--fast`).

How to fix: Re-encode without B-frames (`-bf 0`); `svqa fix` does.

### `video.closed_gop`

**Closed GOPs.** No picture decoded after a keyframe may be displayed before it (open-GOP leading pictures); x264/x265 open_gop setting as extra evidence; the stream must start with a keyframe.

Needs analysis: packets (skipped with `--fast`).

How to fix: Re-encode with closed GOPs; `svqa fix` does.

### `video.keyint`

**Keyframe interval.** Longest distance between keyframes, in seconds (the last GOP runs to the end of the video).

Needs analysis: packets (skipped with `--fast`).

How to fix: Re-encode with a shorter GOP (`-g`); `svqa fix` uses the profile's keyint_s.

### `audio.present`

**Audio track present.** The file has at least one audio stream.

How to fix: Add an audio track (silence is fine); `svqa fix` adds a silent AAC track.

### `audio.codec`

**Audio codec.** FFprobe codec name of the first audio stream (aac, opus, mp3, pcm_s16le, ...).

How to fix: Re-encode the audio to AAC; `svqa fix` does this without touching the video.

### `audio.profile`

**Audio codec profile.** Audio profile per codec (AAC: LC, HE-AAC, HE-AACv2, ...).

How to fix: Re-encode as AAC-LC; `svqa fix` does.

### `audio.sample_rate`

**Audio sample rate.** Sample rate of the first audio stream in Hz.

How to fix: Resample (48000 Hz is the safe choice); `svqa fix` does.

### `audio.channels`

**Audio channels.** Channel count of the first audio stream.

How to fix: Down- or up-mix (stereo is the safe choice); `svqa fix` does.

### `audio.bitrate`

**Audio bitrate.** Average bitrate of the first audio stream in kbps. Skipped when the loudness analysis finds the audio silent, because AAC encodes silence at about 2 kbps whatever bitrate was asked for.

How to fix: Re-encode the audio at the target bitrate; `svqa fix` does (and steps down if AAC overshoots).

### `audio.loudness`

**Integrated loudness.** EBU R128 / ITU-R BS.1770 integrated loudness (FFmpeg ebur128) against target +/- tolerance.

Needs analysis: loudness (skipped with `--fast`).

How to fix: Normalise; `svqa fix` runs two-pass loudnorm and leaves the video untouched if it can.

### `audio.true_peak`

**True peak.** Maximum true peak (4x oversampled, FFmpeg ebur128 peak=true) in dBTP.

Needs analysis: loudness (skipped with `--fast`).

How to fix: Leave headroom for the platform's lossy re-encode; `svqa fix` limits to about -1.5 dBTP.

### `audio.hiss`

**Hiss index (heuristic).** Integrated loudness of the audio after two 6 kHz high-pass filters. An observed heuristic (calibrated 2026-10), not a standard: on mixes normalised to about -14 LUFS, clean audio measured about -40 to -47 and audibly hissy audio about -26 to -30.

Needs analysis: hiss, loudness (skipped with `--fast`).

How to fix: Fix it in the mix (de-ess, low-pass noisy layers, mute noise-based effects). `svqa fix` cannot.

### `audio.silence`

**Audio not silent.** Flags an audio track whose integrated loudness never rises above the -70 LUFS absolute gate.

Needs analysis: loudness (skipped with `--fast`).

How to fix: Check the export settings (muted track?). Intentional silence is fine; set severity to off.

### `content.black_start`

**No black opening frames.** Length of black video at t=0 (FFmpeg blackdetect, 98% of pixels below 10% luma) within the first window_s seconds (default 3).

Needs analysis: start (skipped with `--fast`).

How to fix: Start on a real frame: feeds autoplay from the first frame and often use it as the cover.

### `content.frozen_start`

**No frozen opening.** Length of an unchanging picture at t=0 (FFmpeg freezedetect, -60 dB noise) within the first window_s seconds (default 3). A black opening is reported by content.black_start instead.

Needs analysis: start (skipped with `--fast`).

How to fix: Start with motion: the first second decides whether viewers keep watching.

### `metadata.location`

**No location metadata.** Container or stream tags that carry a recording location (e.g. com.apple.quicktime.location.ISO6709).

How to fix: Strip metadata before publishing; `svqa fix` strips all container metadata by default.
