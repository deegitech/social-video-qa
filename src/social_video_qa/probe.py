"""Measure a media file: streams, container layout, frame timing, loudness, hiss, start content.

Everything here is read-only. Expensive analyses run only when a check needs them:

=========  =============================================================  ============
need       what runs                                                      cost
=========  =============================================================  ============
(always)   ``ffprobe -show_format -show_streams`` + box walk + SEI scan   instant
packets    ``ffprobe -show_entries packet=...`` on the video stream        demux only
loudness   ``ffmpeg -af ebur128=peak=true`` on the audio stream            audio decode
hiss       ``ffmpeg -af highpass=6k,highpass=6k,ebur128``                  audio decode
start      ``blackdetect`` + ``freezedetect`` on the first seconds         short decode
=========  =============================================================  ============
"""

from __future__ import annotations

import json
import os
import re
import stat
import statistics
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Any

from . import boxes as _boxes
from .ffmpeg import FFmpegError, Runner, ffmpeg_reason, hardened_input
from .parsers import (
    Packet,
    Segment,
    parse_blackdetect,
    parse_ebur128,
    parse_encoder_settings,
    parse_fraction,
    parse_freezedetect,
    parse_packets,
)
from .util import round_floats

NEEDS_ALL = frozenset({"packets", "loudness", "hiss", "start"})

#: The hiss index: integrated loudness (EBU R128) of what is left after two cascaded
#: 2-pole 6 kHz high-pass filters (~24 dB/octave). See docs/checks.md#audiohiss.
HISS_FILTER = "highpass=f=6000,highpass=f=6000"

#: ebur128 reports this for programme material that never crosses the absolute gate.
SILENCE_LUFS = -70.0

_LOCATION_KEY = re.compile(r"location|gps|iso6709|xyz", re.IGNORECASE)
#: FFprobe's log line when the demuxer whitelist refuses a file, e.g. "[mp3 @ 0x...] Format not on whitelist".
_NOT_WHITELISTED = re.compile(r"\[([\w.-]+) @ [^\]]*\] Format not on whitelist")


class ProbeError(Exception):
    """The file could not be opened or is not a media file FFprobe understands."""


@dataclass
class VideoInfo:
    index: int
    codec: str | None
    profile: str | None
    level: float | None
    width: int
    height: int
    display_width: int
    display_height: int
    rotation: int
    sar: str | None
    pix_fmt: str | None
    chroma: str | None
    bit_depth: int | None
    field_order: str | None
    r_frame_rate: float | None
    avg_frame_rate: float | None
    bitrate_kbps: float | None
    has_b_frames: int | None
    nb_frames: int | None
    codec_tag: str | None
    color_transfer: str | None
    time_base: str | None

    @property
    def display_aspect(self) -> float | None:
        if not self.display_width or not self.display_height:
            return None
        sar = parse_fraction(self.sar.replace(":", "/")) if self.sar else None
        sar = sar or 1.0
        if self.rotation in (90, 270):
            # display dims are already swapped; SAR applies to the coded width
            return self.display_width / (self.display_height * sar)
        return self.display_width * sar / self.display_height


@dataclass
class AudioInfo:
    index: int
    codec: str | None
    profile: str | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    bitrate_kbps: float | None


@dataclass
class PacketStats:
    """Frame timing and GOP structure of the video stream, from packet timestamps."""

    count: int
    fps: float | None  # 1 / mean frame interval (first and last excluded)
    median_interval_ms: float | None
    reordered: bool  # presentation order differs from decode order (B-frames)
    keyframes: int
    starts_with_keyframe: bool
    max_keyint_s: float | None
    leading_pictures: int  # pictures decoded after a keyframe but shown before it (open GOP)
    avg_kbps: float | None
    peak_kbps_1s: float | None
    tick_s: float = 0.0  # timestamp resolution (stream time base)
    intervals_s: list[float] = field(default_factory=list, repr=False)

    def irregular(self, tolerance_pct: float = 2.0) -> tuple[int, int, float | None]:
        """``(irregular, considered, worst_ms)`` frame intervals, ignoring the first and last.

        The first interval is skipped because MP4 muxers stretch the first frame to
        absorb AAC encoder priming when edit lists are disabled; the last one is
        skipped because its duration is often a guess.
        """
        core = self.intervals_s[1:-1] if len(self.intervals_s) >= 4 else list(self.intervals_s)
        if not core:
            return 0, 0, None
        med = statistics.median(core)
        if med <= 0:
            return len(core), len(core), None
        # One timestamp tick of jitter is rounding, not VFR: WebM stores milliseconds, so 30 fps
        # alternates 33 and 34 ms.
        tol = max(med * tolerance_pct / 100.0, self.tick_s * 1.01)
        bad = [iv for iv in core if abs(iv - med) > tol]
        worst = max(core, key=lambda iv: abs(iv - med))
        return len(bad), len(core), worst * 1000.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("intervals_s", None)
        bad, considered, worst = self.irregular()
        d["irregular_intervals"] = bad
        d["intervals_considered"] = considered
        d["worst_interval_ms"] = worst
        return d


@dataclass
class Loudness:
    integrated_lufs: float | None
    threshold_lufs: float | None
    lra_lu: float | None
    true_peak_dbtp: float | None

    @property
    def silent(self) -> bool:
        i = self.integrated_lufs
        return i is None or i <= SILENCE_LUFS + 0.05


@dataclass
class StartContent:
    window_s: float
    black_start_s: float
    frozen_start_s: float
    black_segments: list[dict[str, float | None]]
    freeze_segments: list[dict[str, float | None]]


@dataclass
class MediaInfo:
    """Everything the checks know about one file."""

    path: str
    size_bytes: int
    format_name: str | None = None
    container: str | None = None
    duration_s: float | None = None
    bitrate_kbps: float | None = None
    video: VideoInfo | None = None
    audio: AudioInfo | None = None
    video_streams: int = 0
    audio_streams: int = 0
    other_streams: int = 0
    boxes: _boxes.BoxInfo | None = None
    encoder_settings: dict[str, Any] | None = None
    tags: dict[str, str] = field(default_factory=dict)
    location_tags: list[str] = field(default_factory=list)
    packets: PacketStats | None = None
    loudness: Loudness | None = None
    hiss_lufs: float | None = None
    start: StartContent | None = None
    analysis_errors: dict[str, str] = field(default_factory=dict)
    measured: set[str] = field(default_factory=set)

    @property
    def video_reordered(self) -> bool:
        """True if the video uses B-frames (or other picture reordering)."""
        if self.packets is not None:
            return self.packets.reordered
        if self.video is not None and self.video.has_b_frames:
            return True
        return bool(self.encoder_settings and (self.encoder_settings.get("bframes") or 0) > 0)

    @property
    def fps(self) -> float | None:
        """Best frame-rate estimate: packet timing, then avg_frame_rate, then r_frame_rate."""
        if self.packets is not None and self.packets.fps:
            return self.packets.fps
        if self.video is None:
            return None
        return self.video.avg_frame_rate or self.video.r_frame_rate

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "format_name": self.format_name,
            "container": self.container,
            "duration_s": self.duration_s,
            "bitrate_kbps": self.bitrate_kbps,
            "streams": {"video": self.video_streams, "audio": self.audio_streams, "other": self.other_streams},
            "video": asdict(self.video) if self.video else None,
            "audio": asdict(self.audio) if self.audio else None,
            "fps": self.fps,
            "boxes": self.boxes.to_dict() if self.boxes else None,
            "encoder_settings": self.encoder_settings,
            "location_tags": self.location_tags,
            "packets": self.packets.to_dict() if self.packets else None,
            "loudness": asdict(self.loudness) if self.loudness else None,
            "hiss_index_lufs": self.hiss_lufs,
            "hiss_relative_lu": (
                self.hiss_lufs - self.loudness.integrated_lufs
                if self.hiss_lufs is not None and self.loudness and self.loudness.integrated_lufs is not None
                else None
            ),
            "start": asdict(self.start) if self.start else None,
            "analysis_errors": self.analysis_errors,
        }
        return round_floats(d)


# ---------------------------------------------------------------------- helpers
def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def chroma_of(pix_fmt: str | None) -> tuple[str | None, int | None]:
    """Chroma subsampling and bit depth from an FFmpeg pixel format name."""
    if not pix_fmt:
        return None, None
    m = re.match(r"^(?:yuvj|yuva|yuv)(\d)(\d)(\d)p(\d+)?", pix_fmt)
    if m:
        a, b, c, depth = m.groups()
        return f"{a}:{b}:{c}", int(depth) if depth else 8
    if pix_fmt in ("nv12", "nv21"):
        return "4:2:0", 8
    m = re.match(r"^p0(\d\d)", pix_fmt)
    if m:
        return "4:2:0", int(m.group(1))
    if pix_fmt.startswith(("nv16", "nv20", "p2")):
        return "4:2:2", None
    if pix_fmt.startswith(("nv24", "nv42", "p4")):
        return "4:4:4", None
    if pix_fmt.startswith("gray"):
        m = re.match(r"^gray(\d+)?", pix_fmt)
        return "4:0:0", int(m.group(1)) if m and m.group(1) else 8
    if pix_fmt.startswith(("rgb", "bgr", "gbr", "argb", "abgr", "0rgb", "0bgr", "x2rgb", "x2bgr")):
        return "4:4:4", None
    return None, None


def normalize_level(codec: str | None, level: Any) -> float | None:
    """FFprobe level number -> decimal level (H.264: 40 -> 4.0, HEVC: 120 -> 4.0)."""
    lv = _int(level)
    if lv is None or lv <= 0:
        return None
    if codec == "h264":
        return round(lv / 10.0, 1)
    if codec == "hevc":
        return round(lv / 30.0, 1)
    return None


def _rotation(stream: dict[str, Any]) -> int:
    rot: Any = None
    for sd in stream.get("side_data_list") or []:
        if isinstance(sd, dict) and "rotation" in sd:
            rot = sd["rotation"]
            break
    if rot is None:
        rot = (stream.get("tags") or {}).get("rotate")
    value = _float(rot)
    if value is None:
        return 0
    return round(value) % 360


def _bitrate_kbps(stream: dict[str, Any]) -> float | None:
    br = _float(stream.get("bit_rate"))
    if br is None:
        tags = {str(k).upper(): v for k, v in (stream.get("tags") or {}).items()}
        br = _float(tags.get("BPS") or tags.get("BPS-ENG"))
    return br / 1000.0 if br else None


def _video_info(s: dict[str, Any]) -> VideoInfo:
    codec = s.get("codec_name")
    width, height = _int(s.get("width")) or 0, _int(s.get("height")) or 0
    rotation = _rotation(s)
    dw, dh = (height, width) if rotation in (90, 270) else (width, height)
    sar = s.get("sample_aspect_ratio")
    chroma, depth = chroma_of(s.get("pix_fmt"))
    return VideoInfo(
        index=_int(s.get("index")) or 0,
        codec=codec,
        profile=s.get("profile"),
        level=normalize_level(codec, s.get("level")),
        width=width,
        height=height,
        display_width=dw,
        display_height=dh,
        rotation=rotation,
        sar=sar if sar not in (None, "N/A", "0:1") else None,
        pix_fmt=s.get("pix_fmt"),
        chroma=chroma,
        bit_depth=depth,
        field_order=s.get("field_order"),
        r_frame_rate=parse_fraction(s.get("r_frame_rate")),
        avg_frame_rate=parse_fraction(s.get("avg_frame_rate")),
        bitrate_kbps=_bitrate_kbps(s),
        has_b_frames=_int(s.get("has_b_frames")),
        nb_frames=_int(s.get("nb_frames")),
        codec_tag=s.get("codec_tag_string"),
        color_transfer=s.get("color_transfer"),
        time_base=s.get("time_base"),
    )


def _audio_info(s: dict[str, Any]) -> AudioInfo:
    return AudioInfo(
        index=_int(s.get("index")) or 0,
        codec=s.get("codec_name"),
        profile=s.get("profile"),
        sample_rate=_int(s.get("sample_rate")),
        channels=_int(s.get("channels")),
        channel_layout=s.get("channel_layout"),
        bitrate_kbps=_bitrate_kbps(s),
    )


def packet_stats(packets: list[Packet], time_base: float, duration_s: float | None = None) -> PacketStats:
    """Derive frame timing and GOP facts from packets listed in decode order."""
    times: list[float] = []
    reordered = False
    max_seen: int | None = None
    last_key: int | None = None
    key_times: list[float] = []
    leading = 0
    total_bytes = 0
    timed: list[tuple[float, int]] = []
    starts_with_key = bool(packets) and packets[0].key
    for p in packets:
        t = p.pts if p.pts is not None else p.dts
        total_bytes += p.size
        if t is None:
            continue
        if max_seen is not None and t < max_seen:
            reordered = True
        max_seen = t if max_seen is None else max(max_seen, t)
        if p.key:
            last_key = t
            key_times.append(t * time_base)
        elif last_key is not None and t < last_key:
            leading += 1
        sec = t * time_base
        times.append(sec)
        dts = p.dts if p.dts is not None else t
        timed.append((dts * time_base, p.size))
    times.sort()
    intervals = [b - a for a, b in pairwise(times)]
    core = intervals[1:-1] if len(intervals) >= 4 else intervals
    median = statistics.median(core) if core else None
    # The mean interval is the true average rate even when timestamps are rounded (WebM's 1 ms
    # ticks make a 30 fps stream alternate 33/34 ms, whose median would read 30.3 fps).
    mean = (sum(core) / len(core)) if core else None
    fps = (1.0 / mean) if mean and mean > 0 else None
    end = (times[-1] + (median or 0.0)) if times else None
    max_keyint = None
    if key_times and end is not None:
        bounds = [*sorted(key_times), end]
        gaps = [b - a for a, b in pairwise(bounds)]
        max_keyint = max(gaps) if gaps else None
    span = duration_s if duration_s and duration_s > 0 else ((end - times[0]) if times and end else None)
    avg_kbps = (total_bytes * 8 / span / 1000.0) if span else None
    peak = None
    if timed:
        timed.sort()
        window_bytes, lo = 0, 0
        best = 0
        for hi in range(len(timed)):
            window_bytes += timed[hi][1]
            while timed[hi][0] - timed[lo][0] >= 1.0:
                window_bytes -= timed[lo][1]
                lo += 1
            best = max(best, window_bytes)
        peak = best * 8 / 1000.0
    return PacketStats(
        count=len(packets),
        fps=fps,
        median_interval_ms=median * 1000.0 if median else None,
        reordered=reordered,
        keyframes=len(key_times),
        starts_with_keyframe=starts_with_key,
        max_keyint_s=max_keyint,
        leading_pictures=leading,
        avg_kbps=avg_kbps,
        peak_kbps_1s=peak,
        tick_s=time_base,
        intervals_s=intervals,
    )


def _container_kind(path: str, format_name: str | None, box_info: _boxes.BoxInfo | None) -> str:
    fmt = format_name or ""
    if box_info is not None and box_info.is_iso and fmt.startswith("mov"):
        return box_info.container_kind()
    if "matroska" in fmt:
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            head = b""
        return "webm" if b"webm" in head[:256] else "mkv"
    if fmt == "avi":
        return "avi"
    if fmt == "mpegts":
        return "ts"
    return fmt.split(",")[0] if fmt else "unknown"


# ---------------------------------------------------------------------- analysis
def _ffprobe_json(runner: Runner, path: str) -> dict[str, Any]:
    args = [runner.ffprobe, "-v", "error", "-hide_banner", *hardened_input(path)]
    args += ["-show_format", "-show_streams", "-of", "json"]
    try:
        proc = runner.run(args, check=False)
    except FFmpegError as exc:
        raise ProbeError(str(exc)) from exc
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        refused = _NOT_WHITELISTED.search(stderr)
        if refused or "not on whitelist" in stderr:
            detected = f" ({refused.group(1)})" if refused else ""
            raise ProbeError(
                f"unsupported container{detected}: svqa only opens MP4/MOV/M4V/3GP, Matroska/WebM, AVI and "
                "MPEG-TS files"
            )
        reason = ffmpeg_reason(stderr, path) or f"exit code {proc.returncode}"
        raise ProbeError(f"ffprobe could not read the file: {reason}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {exc}") from exc
    if not data.get("streams") and not data.get("format"):
        raise ProbeError("ffprobe found no streams")
    return data


def _measure_packets(runner: Runner, path: str, info: MediaInfo) -> None:
    assert info.video is not None
    args = [runner.ffprobe, "-v", "error", "-hide_banner", *hardened_input(path)]
    args += [
        "-select_streams",
        str(info.video.index),
        "-show_entries",
        "packet=pts,dts,duration,size,flags",
        "-of",
        "compact=p=0:nk=0",
    ]
    out = runner.run(args).stdout
    tb = parse_fraction((info.video.time_base or "").replace(":", "/")) or (1 / 90000)
    info.packets = packet_stats(parse_packets(out), tb, info.duration_s)
    if info.video.bitrate_kbps is None and info.packets.avg_kbps:
        info.video.bitrate_kbps = info.packets.avg_kbps


def _ebur128(runner: Runner, path: str, stream_index: int, prefilter: str = "") -> str:
    base = [runner.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-threads", str(runner.threads)]
    base += hardened_input(path)
    base += ["-map", f"0:{stream_index}", "-vn", "-sn", "-dn"]
    chain = (prefilter + "," if prefilter else "") + "ebur128=peak=true:framelog=verbose"
    proc = runner.run([*base, "-af", chain, "-f", "null", "-"], check=False)
    if proc.returncode != 0 and "framelog" in (proc.stderr or ""):
        # FFmpeg builds without the framelog option: same summary, noisier log.
        chain = (prefilter + "," if prefilter else "") + "ebur128=peak=true"
        proc = runner.run([*base, "-af", chain, "-f", "null", "-"], check=False)
    if proc.returncode != 0:
        reason = ffmpeg_reason(proc.stderr, path) or f"exit code {proc.returncode}"
        raise FFmpegError(f"ebur128 analysis failed: {reason}")
    return proc.stderr


def _measure_loudness(runner: Runner, path: str, info: MediaInfo) -> None:
    assert info.audio is not None
    summary = parse_ebur128(_ebur128(runner, path, info.audio.index))
    if summary is None:
        raise FFmpegError("ebur128 printed no summary")
    info.loudness = Loudness(
        integrated_lufs=summary.integrated_lufs,
        threshold_lufs=summary.threshold_lufs,
        lra_lu=summary.lra_lu,
        true_peak_dbtp=summary.true_peak_dbtp,
    )


def _measure_hiss(runner: Runner, path: str, info: MediaInfo) -> None:
    assert info.audio is not None
    summary = parse_ebur128(_ebur128(runner, path, info.audio.index, HISS_FILTER))
    if summary is None:
        raise FFmpegError("ebur128 printed no summary for the hiss band")
    info.hiss_lufs = summary.integrated_lufs


def _measure_start(runner: Runner, path: str, info: MediaInfo, window_s: float) -> None:
    assert info.video is not None
    args = [runner.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-threads", str(runner.threads)]
    args += ["-t", f"{window_s:g}", *hardened_input(path)]
    args += ["-map", f"0:{info.video.index}", "-an", "-sn", "-dn"]
    args += ["-vf", "blackdetect=d=0:pic_th=0.98:pix_th=0.10,freezedetect=n=-60dB:d=0.1", "-f", "null", "-"]
    err = runner.run(args).stderr
    frame = 1.0 / (info.fps or 30.0)
    window = min(window_s, info.duration_s or window_s)
    # Keyframes re-quantise a still picture slightly, which splits one freeze into touching
    # segments; merge segments whose gap is under two frames.
    blacks = merge_segments(parse_blackdetect(err), window, 2 * frame)
    freezes = merge_segments(parse_freezedetect(err), window, 2 * frame)
    black_start = next((s.end - s.start for s in blacks if s.start <= frame * 0.5 and s.end is not None), 0.0)
    frozen_start = next((s.end - s.start for s in freezes if s.start <= frame * 0.5 and s.end is not None), 0.0)
    info.start = StartContent(
        window_s=window_s,
        black_start_s=black_start,
        frozen_start_s=frozen_start,
        black_segments=[asdict(s) for s in blacks],
        freeze_segments=[asdict(s) for s in freezes],
    )


def merge_segments(segments: list[Segment], window: float, gap: float) -> list[Segment]:
    """Close open-ended segments at ``window`` and merge segments separated by less than ``gap``."""
    closed = []
    for seg in segments:
        end = seg.end if seg.end is not None else (seg.start + seg.duration if seg.duration is not None else window)
        closed.append(Segment(seg.start, end, end - seg.start))
    closed.sort(key=lambda x: x.start)
    merged: list[Segment] = []
    for seg in closed:
        if merged and merged[-1].end is not None and seg.start - merged[-1].end <= gap:
            last = merged[-1]
            end = max(last.end or 0.0, seg.end or 0.0)
            merged[-1] = Segment(last.start, end, end - last.start)
        else:
            merged.append(seg)
    return merged


def analyze(
    path: str,
    runner: Runner,
    needs: frozenset[str] | set[str] = NEEDS_ALL,
    *,
    start_window_s: float = 3.0,
) -> MediaInfo:
    """Measure ``path``. Raises :class:`ProbeError` if it cannot be read at all.

    Failures of individual analyses (for example a missing FFmpeg filter) are
    recorded in ``MediaInfo.analysis_errors`` so the remaining checks still run.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ProbeError(f"cannot open: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ProbeError("not a regular file")
    if st.st_size == 0:
        raise ProbeError("file is empty")
    data = _ffprobe_json(runner, path)
    fmt = data.get("format") or {}
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    info = MediaInfo(path=path, size_bytes=st.st_size, format_name=fmt.get("format_name"))

    videos = [
        s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    info.video_streams, info.audio_streams = len(videos), len(audios)
    info.other_streams = len(streams) - len(videos) - len(audios)
    info.video = _video_info(videos[0]) if videos else None
    info.audio = _audio_info(audios[0]) if audios else None

    durations = [_float(fmt.get("duration"))] + [_float(s.get("duration")) for s in videos + audios]
    known = [d for d in durations if d and d > 0]
    info.duration_s = known[0] if known else None
    br = _float(fmt.get("bit_rate"))
    if br:
        info.bitrate_kbps = br / 1000.0
    elif info.duration_s:
        info.bitrate_kbps = st.st_size * 8 / info.duration_s / 1000.0

    tags: dict[str, str] = {}
    for scope in [fmt, *streams]:
        for k, v in (scope.get("tags") or {}).items():
            tags.setdefault(str(k).lower(), str(v))
    info.tags = tags
    info.location_tags = sorted(k for k in tags if _LOCATION_KEY.search(k))

    try:
        info.boxes = _boxes.parse_file(path)
    except OSError as exc:
        info.analysis_errors["boxes"] = str(exc)
    info.container = _container_kind(path, info.format_name, info.boxes)
    try:
        with open(path, "rb") as f:
            info.encoder_settings = parse_encoder_settings(f.read(4 << 20))
    except OSError:
        info.encoder_settings = None

    steps = [
        ("packets", info.video is not None, lambda: _measure_packets(runner, path, info)),
        ("loudness", info.audio is not None, lambda: _measure_loudness(runner, path, info)),
        ("hiss", info.audio is not None, lambda: _measure_hiss(runner, path, info)),
        ("start", info.video is not None, lambda: _measure_start(runner, path, info, start_window_s)),
    ]
    for name, applicable, fn in steps:
        if name not in needs or not applicable:
            continue
        try:
            fn()
            info.measured.add(name)
        except FFmpegError as exc:
            info.analysis_errors[name] = str(exc)
    return info
