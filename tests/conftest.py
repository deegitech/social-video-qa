"""Shared fixtures.

Unit tests build :class:`MediaInfo` objects by hand and never touch FFmpeg.
Integration tests generate tiny synthetic videos with FFmpeg's lavfi sources
(testsrc2, sine, anoisesrc, color) and are skipped when ffmpeg/ffprobe are missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable

import pytest

from social_video_qa.boxes import BoxInfo, Track
from social_video_qa.ffmpeg import Runner
from social_video_qa.probe import AudioInfo, Loudness, MediaInfo, PacketStats, StartContent, VideoInfo

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


# ---------------------------------------------------------------------- offline factory
def make_media(**overrides: object) -> MediaInfo:
    """A plausible, fully passing 1080x1920 30 fps H.264/AAC Reel (no FFmpeg involved)."""
    video = VideoInfo(
        index=0,
        codec="h264",
        profile="High",
        level=4.0,
        width=1080,
        height=1920,
        display_width=1080,
        display_height=1920,
        rotation=0,
        sar=None,
        pix_fmt="yuv420p",
        chroma="4:2:0",
        bit_depth=8,
        field_order="progressive",
        r_frame_rate=30.0,
        avg_frame_rate=30.0,
        bitrate_kbps=5000.0,
        has_b_frames=0,
        nb_frames=300,
        codec_tag="avc1",
        color_transfer=None,
        time_base="1/15360",
    )
    audio = AudioInfo(
        index=1, codec="aac", profile="LC", sample_rate=48000, channels=2, channel_layout="stereo", bitrate_kbps=128.0
    )
    boxes = BoxInfo(
        is_iso=True,
        top_level=["ftyp", "moov", "free", "mdat"],
        major_brand="isom",
        compatible_brands=["isom", "iso2", "avc1", "mp41"],
        moov_before_mdat=True,
        tracks=[Track(1, "vide", True, 0), Track(2, "soun", True, 0)],
    )
    packets = PacketStats(
        count=300,
        fps=30.0,
        median_interval_ms=1000 / 30,
        reordered=False,
        keyframes=5,
        starts_with_keyframe=True,
        max_keyint_s=2.0,
        leading_pictures=0,
        avg_kbps=5000.0,
        peak_kbps_1s=6200.0,
        intervals_s=[1 / 30] * 299,
    )
    m = MediaInfo(
        path="clip.mp4",
        size_bytes=6_600_000,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        container="mp4",
        duration_s=10.0,
        bitrate_kbps=5200.0,
        video=video,
        audio=audio,
        video_streams=1,
        audio_streams=1,
        other_streams=0,
        boxes=boxes,
        encoder_settings={"encoder": "x264", "bframes": 0, "open_gop": 0, "keyint": 60},
        packets=packets,
        loudness=Loudness(integrated_lufs=-14.0, threshold_lufs=-24.0, lra_lu=5.0, true_peak_dbtp=-1.6),
        hiss_lufs=-45.0,
        start=StartContent(window_s=3.0, black_start_s=0.0, frozen_start_s=0.0, black_segments=[], freeze_segments=[]),
        measured={"packets", "loudness", "hiss", "start"},
    )
    for key, value in overrides.items():
        setattr(m, key, value)
    return m


@pytest.fixture
def media() -> MediaInfo:
    return make_media()


# ---------------------------------------------------------------------- FFmpeg helpers
def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def runner() -> Runner:
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    return Runner.discover(threads=2, nice=0, timeout=120)


@pytest.fixture(scope="session")
def ffmpeg_version(runner: Runner) -> tuple[int, int]:
    return runner.version()[1] or (99, 0)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("media"))


#: x264 settings that produce a file Instagram's API spec is happy with.
SAFE_VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
              "-bf", "0", "-g", "30", "-flags", "+cgop"]  # fmt: skip
SAFE_AUDIO = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
SAFE_MUX = ["-use_editlist", "0", "-movflags", "+faststart"]
#: sine at about -14 LUFS once K-weighted and summed over two channels
LEVELLED_SINE = "loudnorm=I=-14:TP=-2:LRA=11,aresample=48000"


@pytest.fixture(scope="session")
def make(media_dir: str, runner: Runner) -> Callable[..., str]:
    """``make(name, *ffmpeg_args)`` -> path; each file is generated once per session."""
    cache: dict[str, str] = {}

    def build(name: str, *args: str) -> str:
        if name not in cache:
            path = os.path.join(media_dir, name)
            ffmpeg(*args, path)
            cache[name] = path
        return cache[name]

    return build


def src_video(size: str = "360x640", rate: int = 30, seconds: float = 4) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]


def src_sine(seconds: float = 4, rate: int = 48000, freq: int = 440) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:duration={seconds}"]


@pytest.fixture(scope="session")
def clean_reel(make: Callable[..., str]) -> str:
    """Passes instagram-reels-api without errors or warnings (only the 1080x1920 info note)."""
    return make(
        "clean.mp4", *src_video(), *src_sine(), "-af", LEVELLED_SINE, *SAFE_VIDEO, *SAFE_AUDIO, *SAFE_MUX, "-shortest"
    )
