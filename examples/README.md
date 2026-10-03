# Examples

## Profiles

| File | Shows |
| --- | --- |
| [`profiles/brand-reels.json`](profiles/brand-reels.json) | A stricter house style on top of `instagram-reels-api`: exact size and frame rate, tighter loudness, hiss as an error, a rule removed with `null`, and a `fix` override. |
| [`profiles/app-store-preview-iphone-5.5.yaml`](profiles/app-store-preview-iphone-5.5.yaml) | Reusing the App Store rules for another device size (YAML, needs PyYAML). |
| [`profiles/shorts-60s.toml`](profiles/shorts-60s.toml) | A team rule for YouTube Shorts, and switching a check off with `severity = "off"` (TOML: Python 3.11+, or the `toml` extra on 3.10). |

Try them:

```sh
svqa profiles validate examples/profiles/*
svqa profiles show examples/profiles/brand-reels.json
svqa check clip.mp4 -p brand-reels --profile-dir examples/profiles
```

## CI

[`github-actions.yml`](github-actions.yml) is a complete GitHub Actions workflow. It checks every exported video in a
pull request against two platforms and publishes the JUnit report. Copy it to `.github/workflows/` in your own project
and adjust the folder and profiles.

## Make a test clip

You don't need real footage to try the tool. FFmpeg can generate a clip:

```sh
ffmpeg -f lavfi -i testsrc2=size=1080x1920:rate=30:duration=12 \
       -f lavfi -i sine=frequency=440:sample_rate=48000:duration=12 \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest test-clip.mp4
svqa check test-clip.mp4
```
