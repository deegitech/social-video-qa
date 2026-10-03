"""Minimal ISO base media file format (MP4/MOV) box reader.

FFprobe does not report *where* the ``moov`` box sits or whether tracks carry an
edit list (``elst``), and both matter to upload pipelines: Instagram's Graph API
asks for "no edit lists, moov atom at the front of the file" and YouTube's
recommended settings say the same. This module walks the box tree directly. It
reads only box headers at the top level and the ``moov`` payload, so it is fast
even on multi-gigabyte files.
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass, field
from typing import BinaryIO

#: Boxes whose payload is just more boxes.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex"}
#: Refuse to load absurdly large ``moov`` payloads (corrupt size fields).
MAX_MOOV_BYTES = 256 * 1024 * 1024
#: Real files nest about six levels deep; anything far deeper is crafted.
MAX_DEPTH = 32
#: Top-level types that mark a file as ISO-BMFF when seen first.
_ISO_FIRST = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"styp", b"uuid"}


@dataclass
class Track:
    """What we learn about one ``trak`` box."""

    track_id: int | None = None
    handler: str | None = None  # 'vide', 'soun', 'tmcd', 'meta', ...
    enabled: bool | None = None  # tkhd flag 0x1
    edit_list_entries: int = 0  # 0 means no elst box


@dataclass
class BoxInfo:
    """Layout facts about an MP4/MOV file."""

    is_iso: bool = False
    top_level: list[str] = field(default_factory=list)
    major_brand: str | None = None
    compatible_brands: list[str] = field(default_factory=list)
    moov_before_mdat: bool | None = None
    fragmented: bool = False
    truncated: bool = False
    moov_unreadable: bool = False
    tracks: list[Track] = field(default_factory=list)

    @property
    def tracks_with_edit_lists(self) -> list[Track]:
        return [t for t in self.tracks if t.edit_list_entries > 0]

    @property
    def disabled_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.enabled is False]

    def container_kind(self) -> str:
        """``mov``, ``m4v``, ``3gp`` or ``mp4`` from the ``ftyp`` brands."""
        major = self.major_brand or ""
        if major == "qt  " or (not major and "moov" in self.top_level):
            return "mov"
        if major.startswith("M4V"):
            return "m4v"
        if major.startswith("3g"):
            return "3gp"
        return "mp4"

    def to_dict(self) -> dict:
        return {
            "is_iso": self.is_iso,
            "top_level": self.top_level[:50],
            "major_brand": self.major_brand,
            "compatible_brands": self.compatible_brands,
            "moov_before_mdat": self.moov_before_mdat,
            "fragmented": self.fragmented,
            "truncated": self.truncated,
            "tracks": [
                {
                    "track_id": t.track_id,
                    "handler": t.handler,
                    "enabled": t.enabled,
                    "edit_list_entries": t.edit_list_entries,
                }
                for t in self.tracks
            ],
        }


def _read_header(f: BinaryIO, pos: int, end: int) -> tuple[int, bytes, int] | None:
    """Return ``(size, type, header_len)`` of the box at ``pos`` or ``None`` if unreadable."""
    if pos + 8 > end:
        return None
    f.seek(pos)
    head = f.read(8)
    if len(head) < 8:
        return None
    size, typ = struct.unpack(">I4s", head)
    header = 8
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = struct.unpack(">Q", ext)[0]
        header = 16
    elif size == 0:
        size = end - pos
    return size, typ, header


def _walk(buf: bytes, track: Track | None, tracks: list[Track], depth: int = 0) -> None:
    """Walk boxes inside a ``moov`` payload, filling ``tracks``."""
    if depth > MAX_DEPTH:
        return
    pos, n = 0, len(buf)
    while pos + 8 <= n:
        size, typ = struct.unpack(">I4s", buf[pos : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > n:
                return
            size = struct.unpack(">Q", buf[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = n - pos
        if size < header or pos + size > n:
            return
        body = buf[pos + header : pos + size]
        if typ == b"trak":
            t = Track()
            tracks.append(t)
            _walk(body, t, tracks, depth + 1)
        elif typ in _CONTAINERS:
            _walk(body, track, tracks, depth + 1)
        elif track is not None and typ == b"tkhd" and len(body) >= 4:
            version = body[0]
            flags = int.from_bytes(body[1:4], "big")
            track.enabled = bool(flags & 0x1)
            # tkhd v0: 4 creation + 4 modification + 4 track_ID; v1 uses 8-byte times.
            off = 4 + (16 if version == 1 else 8)
            if len(body) >= off + 4:
                track.track_id = struct.unpack(">I", body[off : off + 4])[0]
        elif track is not None and typ == b"elst" and len(body) >= 8:
            track.edit_list_entries = max(1, struct.unpack(">I", body[4:8])[0])
        elif track is not None and typ == b"hdlr" and len(body) >= 12 and track.handler is None:
            # QuickTime files also have a data-handler hdlr in minf (component type 'dhlr', subtype
            # 'url ' or 'alis'). The media handler in mdia comes first and is the one we want.
            if body[4:8] != b"dhlr":
                track.handler = body[8:12].decode("latin-1")
        pos += size


def parse_stream(f: BinaryIO, size: int) -> BoxInfo:
    """Parse an open binary stream of ``size`` bytes."""
    info = BoxInfo()
    first = _read_header(f, 0, size)
    if first is None or first[1] not in _ISO_FIRST:
        return info
    info.is_iso = True
    pos = 0
    moov_payload: bytes | None = None
    while pos < size:
        hdr = _read_header(f, pos, size)
        if hdr is None:
            info.truncated = pos < size
            break
        box_size, typ, header = hdr
        if box_size < header:
            info.truncated = True
            break
        name = typ.decode("latin-1")
        info.top_level.append(name)
        if pos + box_size > size:
            info.truncated = True
        if typ == b"ftyp":
            f.seek(pos + header)
            body = f.read(min(box_size - header, 1024))
            if len(body) >= 8:
                info.major_brand = body[0:4].decode("latin-1")
                info.compatible_brands = [body[i : i + 4].decode("latin-1") for i in range(8, len(body) - 3, 4)]
        elif typ == b"moov" and moov_payload is None:
            length = box_size - header
            if length > MAX_MOOV_BYTES:
                info.moov_unreadable = True
            else:
                f.seek(pos + header)
                moov_payload = f.read(length)
        elif typ == b"moof":
            info.fragmented = True
        pos += box_size
    if "moov" in info.top_level and "mdat" in info.top_level:
        info.moov_before_mdat = info.top_level.index("moov") < info.top_level.index("mdat")
    if moov_payload:
        _walk(moov_payload, None, info.tracks)
    return info


def parse_file(path: str) -> BoxInfo:
    """Parse the box layout of the file at ``path``."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        return parse_stream(f, size)


def parse_bytes(data: bytes) -> BoxInfo:
    """Parse an in-memory file (used by tests)."""
    return parse_stream(io.BytesIO(data), len(data))


# ---------------------------------------------------------------------- builders
def box(typ: bytes, payload: bytes = b"", *, large: bool = False) -> bytes:
    """Build a box (handy for tests and fixtures)."""
    if large:
        return struct.pack(">I4sQ", 1, typ, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def full_box(typ: bytes, version: int, flags: int, payload: bytes = b"") -> bytes:
    """Build a 'full box' (version + 24-bit flags header)."""
    return box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)
