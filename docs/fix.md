# How `svqa fix` works

`svqa fix` produces a copy of your video that passes a profile, changing as little as possible. It never modifies the
input, and it writes nothing unless you pass `--apply`.

## The pipeline

1. **Analyse and check** the source against the profile, exactly like `svqa check`.
2. **Plan.** Every failing check at `error` or `warn` level is mapped to the cheapest repair that fixes it (see below).
   Failures that cannot be fixed automatically (hiss, a black or frozen opening, a video that is too short) are listed.
   If one of them is an `error` (or a warning, with `--strict`), the fix is **refused** before any work is done, because
   the result could not pass.
3. **Dry run stops here** and prints the plan. Add `--show-commands` to see the exact `ffmpeg` commands.
4. With `--apply`:
   1. take a lock on the output folder (`.svqa-manifest.lock`), so two runs never write the same folder (not with
      `--no-manifest`, which keeps no journal and takes no lock);
   2. record the intent in `.svqa-manifest.json` (status `encoding`, source SHA-256, profile hash, recipe hash);
   3. measure loudness (first `loudnorm` pass), if the audio will be re-encoded and the profile has a loudness rule;
   4. encode into a hidden temporary file in the output folder;
   5. if the profile caps the audio bitrate and the AAC encoder overshot it, re-encode only the audio, from the source,
      in steps of 12.5% lower (at most twice);
   6. **re-check** the temporary file against the same profile;
   7. if no error-level check fails (no warning either, with `--strict`), move it into place with an atomic rename and
      record status `ok` with the output's SHA-256; otherwise delete it (or keep it as `*.failed.mp4` with
      `--keep-failed`) and record `failed`.

Temporary files are removed on errors and on Ctrl-C (the manifest then says `failed` or `interrupted`). If the folder is
locked by another run, or its manifest is unreadable, that file is reported as an error and the batch goes on.

## Output names

The fixed copy is `<name>.<profile>.mp4`, next to the input or in `-O DIR`. With `-O`, files found inside a folder keep
their sub-folder: `svqa fix previews/ -O upload/` writes `previews/a/clip.mp4` to `upload/a/clip.<profile>.mp4`. If two
inputs would still write the same file (`intro.mov` and `intro.mp4` in one folder, or the same name given from two
folders), the second one is refused; rename one of them, or fix that one on its own with `-o FILE` or another
`-O DIR`.

Files that already pass are reported as `nothing to do` and are not copied, so `-O DIR` contains only the files that
needed a fix.

## Modes

| Mode | What happens | Typical triggers |
| --- | --- | --- |
| `remux` | Both streams are copied bit for bit into a new MP4: `moov` first, no edit lists, metadata stripped, extra streams dropped. Lossless and fast. | `container.faststart`, `container.edit_list` (when the video has no B-frames), `container.format`, `container.streams`, `metadata.location` |
| `audio` | The video is copied; the audio is re-encoded to AAC with two-pass loudness normalisation, or a silent track is added. | `audio.loudness`, `audio.true_peak`, `audio.codec`, `audio.sample_rate`, `audio.channels`, `audio.bitrate`, `audio.present` |
| `full` | The video is re-encoded with libx264 (audio too when it needs it, or when trimming). | anything under `video.*`, `bitrate.total`, `file.size`, `duration` with `--trim` |

`--mode auto` (the default) picks the cheapest mode that covers every fixable failure. Two cases escalate on purpose:

- **B-frames with a no-edit-list rule.** Removing the edit list from a B-frame stream without re-encoding would shift the
  video against the audio by a couple of frames, so the video is re-encoded without B-frames instead.
- **Codecs that MP4 cannot carry**, such as ProRes video or PCM audio, are re-encoded.

`--mode remux|audio|full` forces a mode. If the forced mode cannot fix something, the plan says so and is refused.

## Encode settings

The profile's `fix` section sets the targets, and the rules cap them:

- **Size.** The source size is kept when it passes every size rule. Otherwise the target is `fix.size`, or else the first
  allowed or recommended size. If the aspect ratio differs, `--fit` decides: `pad` adds bars (default), `crop` fills and
  cuts, and `blur` places the video on a blurred, zoomed copy of itself. Rotation metadata is applied to the pixels,
  non-square (anamorphic) pixels are resampled to square ones first, and interlaced sources are de-interlaced (`bwdif`).
- **HDR.** HDR sources (PQ or HLG, which iPhones record by default) are converted to 8-bit without tone mapping, so
  colours and highlights can look wrong. The plan warns about it; export SDR from your editor for the best result.
- **Frame rate.** The source rate is kept when it is allowed. Otherwise the tool picks a standard rate inside the allowed
  range that keeps the cadence: 59.94 becomes 29.97, 50 becomes 25, and 120 becomes 60. The output is always constant
  frame rate.
- **Video.** libx264 with the profile's H.264 profile and level, `-bf 0` (no B-frames), closed GOPs every `keyint_s`
  seconds, yuv420p. Quality-based profiles use CRF with a VBV cap (`maxrate_kbps`), which is lowered when a bitrate rule
  or the file-size limit needs it. The size budget is the size limit over the duration, minus the audio, with an 8%
  margin. Profiles with a bitrate target (App Store previews) use constant bitrate (`nal-hrd=cbr`).
- **Audio.** AAC at the profile's bitrate, capped by `audio.bitrate`, at 48 kHz stereo unless the rules say otherwise.
  Loudness is normalised in two passes with FFmpeg's `loudnorm` in linear mode, to the profile's target and to a true
  peak 0.5 dB under the profile's limit. If the source's loudness range is wider than the target, the target range is
  raised so that linear mode is still possible. Silent tracks are left alone.
- **Container.** MP4 with `-movflags +faststart`, `-use_editlist 0`, and `-map_metadata -1` for global and stream
  metadata. HEVC that is copied gets the `hvc1` tag that Apple players need.
- **Trimming** (`--trim`) cuts at the profile's maximum minus 0.1 s and fades the audio out over the last 0.3 s.

## Idempotency and the manifest

`.svqa-manifest.json` in each output folder maps output file names to:

```json
{
  "status": "ok",
  "source": "clip.mp4",
  "source_path_sha256": "…",
  "source_sha256": "…",
  "profile": "instagram-reels-api",
  "profile_sha256": "…",
  "recipe": "…",
  "mode": "full",
  "output_sha256": "…",
  "tool_version": "0.1.0",
  "ffmpeg_version": "7.1",
  "started_at": "2030-01-15T12:00:00Z",
  "finished_at": "2030-01-15T12:00:06Z",
  "verification": {"errors": 0, "warnings": 1, "warned_checks": ["audio.hiss"]}
}
```

A re-run with the same source, profile and settings is reported as `up to date` and skipped (`--force` re-encodes).
With `--strict`, an up-to-date output whose verification had warnings is reported as failed instead.

An existing output is never overwritten without `--force` when it is not in the manifest, when its hash no longer
matches, or when it was written from a different source, that is a file with both another path and other content. The
source's resolved path is recorded only as a hash (`source_path_sha256`). While a re-encode runs, and after it fails or is
interrupted, the entry keeps the `output_sha256` of the file that is still published, so the next run can replace it
without `--force`.

When you point `fix` at a folder, outputs listed in that folder's manifest are not treated as new inputs.
`--no-manifest` turns the journal off; outputs then always need `--force` to be replaced.

## CPU use

FFmpeg runs with `--threads 2` per stage (decoding, filtering, encoding) and, on macOS and Linux, under `nice 10`. A
full re-encode can still use 2 to 4 cores in total, because the stages run in parallel. Use `--threads 1` on a busy
machine, or a faster `--preset` when time matters more than file size. `--timeout` bounds each analysis step, not the
encode itself; on a server, wrap `svqa fix` in `timeout(1)` or a container CPU/time limit.
