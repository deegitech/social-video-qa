# Writing profiles

A profile is a JSON, YAML or TOML file. The built-in profiles in
[`src/social_video_qa/data/profiles/`](../src/social_video_qa/data/profiles/) are good starting points.

## Minimal example

```json
{
  "id": "house-reels",
  "extends": "instagram-reels-api",
  "title": "House Reels",
  "rules": {
    "duration": {"max_s": 90},
    "video.fps": {"min": 30, "max": 30},
    "audio.hiss": null
  }
}
```

Use it by path (`-p ./house-reels.json`), or put it in a folder and use its ID (`-p house-reels --profile-dir folder`,
or set `SVQA_PROFILE_PATH`). A file in a profile folder can even shadow a built-in ID. If it `extends` that same ID, it
inherits from the built-in, so `tiktok.json` with `"extends": "tiktok"` works.

Check your file with `svqa profiles validate house-reels.json`, and see the resolved result with
`svqa profiles show ./house-reels.json`.

## Top-level keys

| Key | Type | Notes |
| --- | --- | --- |
| `id` | string | Lowercase letters, digits, `.`, `_`, `-`. Defaults to the file name. |
| `extends` | string or list | Profile IDs or relative file paths. Parents merge left to right, then this file. |
| `title`, `description`, `platform` | string | Shown by `svqa profiles` and in reports. A child never inherits `title` or `description`. |
| `sources` | list of `{title, url, checked, note}` | Where the numbers come from. |
| `notes` | list of strings | Caveats and uncertainty, shown by `svqa profiles show`. |
| `rules` | mapping | Check ID to parameters, see below. |
| `fix` | mapping | Encode settings for `svqa fix`, see below. |
| `x-…` | anything | Ignored; use it for your own metadata. |

Unknown keys are errors, so typos fail loudly instead of being ignored.

## Merging

- Mappings merge key by key: `"duration": {"max_s": 90}` keeps the parent's `min_s`.
- Lists and plain values replace the parent's value.
- `null` deletes the inherited key: `"audio.hiss": null` switches that check off, and `"fix": {"loudness": null}`
  disables loudness normalisation.

## Rules

Each key under `rules` is a check ID and its value is the check's parameters (`{}` enables a check that has none). Every
rule also accepts:

| Key | Meaning |
| --- | --- |
| `severity` | `error`, `warn`, `info` or `off`. Defaults to the check's default (see [checks.md](checks.md)). |
| `basis` | `documented`, `observed`, `convention` or `derived`: where the rule comes from. |
| `note` | Shown next to failures and in `profiles show`. |
| `source` | Free text, for example a URL. |

Range parameters come in hard and soft pairs: `min_s`/`max_s` fail at the rule's severity, while
`recommended_min_s`/`recommended_max_s` only warn. List parameters work the same way: `allowed` versus `recommended`.

| Check | Parameters |
| --- | --- |
| `file.size` | `min_mb`, `max_mb` (decimal MB) |
| `container.format` | `allowed`, `recommended`: `mp4`, `mov`, `m4v`, `3gp`, `webm`, `mkv`, `avi`, `ts` |
| `container.faststart`, `container.edit_list`, `container.tracks_enabled` | none |
| `container.streams` | `max_video`, `max_audio` |
| `duration` | `min_s`, `max_s`, `recommended_min_s`, `recommended_max_s` |
| `bitrate.total`, `video.bitrate`, `audio.bitrate` | `min_kbps`, `max_kbps`, `recommended_min_kbps`, `recommended_max_kbps` |
| `video.codec`, `audio.codec` | `allowed`, `recommended` (FFprobe codec names: `h264`, `hevc`, `vp9`, `aac`, `opus`, ...) |
| `video.profile`, `audio.profile` | `allowed`: mapping of codec to profile names, e.g. `{"h264": ["High", "Main"]}` |
| `video.level` | `max`: mapping of codec to decimal level, e.g. `{"h264": 4.0}` |
| `video.pix_fmt` | `allowed`, `recommended` (`yuv420p`, ...) |
| `video.chroma` | `allowed`, `recommended` (`4:2:0`, `4:2:2`, `4:4:4`) |
| `video.progressive`, `video.sar`, `video.rotation`, `video.b_frames`, `video.closed_gop` | none |
| `video.size` | `allowed`, `recommended`: lists of `"WxH"` |
| `video.resolution` | `min_width`, `max_width`, `min_height`, `max_height` |
| `video.aspect` | `min`, `max` (width/height, a number or `"W:H"`), `recommended` (one or a list), `tolerance` (default 0.01) |
| `video.fps` | `min`, `max`, `recommended_min`, `recommended_max` |
| `video.cfr` | `tolerance_pct` (default 2), `max_irregular_pct` (default 0) |
| `video.keyint` | `max_s` (required) |
| `audio.present`, `audio.silence`, `metadata.location` | none |
| `audio.sample_rate` | `allowed`, `recommended` (Hz), `min_hz`, `max_hz` |
| `audio.channels` | `allowed`, `recommended` |
| `audio.loudness` | `target_lufs` (required), `tolerance_lu` (default 1) |
| `audio.true_peak` | `max_dbtp` (required) |
| `audio.hiss` | `max_lufs`, `max_relative_lu` |
| `content.black_start`, `content.frozen_start` | `max_s`, `window_s` (seconds analysed, default 3) |

## The `fix` section

All keys are optional; missing ones fall back to the defaults shown.

```json
"fix": {
  "size": "1080x1920",
  "fit": "pad",
  "pad_color": "black",
  "fps": "auto",
  "strip_metadata": true,
  "video": {"encoder": "libx264", "profile": "high", "level": "4.0", "preset": "medium", "crf": 20,
            "maxrate_kbps": 8000, "bufsize_kbps": 16000, "cbr_kbps": null, "bframes": 0, "keyint_s": 2,
            "pix_fmt": "yuv420p"},
  "audio": {"encoder": "aac", "bitrate_kbps": 128, "sample_rate": 48000, "channels": 2, "add_silent_track": false},
  "loudness": {"target_lufs": -14, "true_peak_dbtp": -1.5, "lra": 11},
  "container": {"format": "mp4", "faststart": true, "edit_list": false}
}
```

- `size` is used only when the source fails a size rule. `fps: "auto"` keeps an allowed source rate, or picks a
  cadence-friendly one; a number forces it.
- `cbr_kbps` switches from CRF to constant bitrate.
- `level` is omitted by default (x264 picks one).
- The loudness target comes from the `audio.loudness` rule when there is one.
- `add_silent_track` adds silence when the source has no audio. That happens anyway when `audio.present` is a rule.

## YAML and TOML

YAML needs PyYAML (`pip install "social-video-qa[yaml]"`); TOML needs Python 3.11+ (on 3.10,
`pip install "social-video-qa[toml]"`). See [examples/profiles/](../examples/profiles/) for one of each.
