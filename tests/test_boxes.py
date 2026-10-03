"""MP4/MOV box walking on hand-built byte strings (no FFmpeg)."""

from __future__ import annotations

import struct

from social_video_qa.boxes import box, full_box, parse_bytes, parse_file


def ftyp(major: bytes = b"isom", compat: tuple[bytes, ...] = (b"isom", b"mp41")) -> bytes:
    return box(b"ftyp", major + struct.pack(">I", 512) + b"".join(compat))


def tkhd(enabled: bool = True, track_id: int = 1, version: int = 0) -> bytes:
    times = b"\x00" * (16 if version == 1 else 8)
    return full_box(b"tkhd", version, 0x3 if enabled else 0x2, times + struct.pack(">I", track_id) + b"\x00" * 60)


def hdlr(kind: bytes) -> bytes:
    return full_box(b"hdlr", 0, 0, b"\x00" * 4 + kind + b"\x00" * 12 + b"Handler\x00")


def elst(entries: int = 1) -> bytes:
    return full_box(b"elst", 0, 0, struct.pack(">I", entries) + b"\x00" * 12 * entries)


def trak(kind: bytes, *, enabled: bool = True, edit: bool = False, track_id: int = 1) -> bytes:
    children = tkhd(enabled, track_id)
    if edit:
        children += box(b"edts", elst())
    children += box(b"mdia", hdlr(kind) + box(b"minf", box(b"stbl", b"")))
    return box(b"trak", children)


def moov(*traks: bytes) -> bytes:
    return box(b"moov", full_box(b"mvhd", 0, 0, b"\x00" * 96) + b"".join(traks))


MDAT = box(b"mdat", b"\x00" * 64)


def test_faststart_layout_and_tracks() -> None:
    info = parse_bytes(ftyp() + moov(trak(b"vide", track_id=1), trak(b"soun", track_id=2)) + MDAT)
    assert info.is_iso
    assert info.top_level == ["ftyp", "moov", "mdat"]
    assert info.moov_before_mdat is True
    assert [t.handler for t in info.tracks] == ["vide", "soun"]
    assert [t.track_id for t in info.tracks] == [1, 2]
    assert all(t.enabled for t in info.tracks)
    assert info.tracks_with_edit_lists == []
    assert info.container_kind() == "mp4"
    assert info.major_brand == "isom"
    assert info.compatible_brands == ["isom", "mp41"]


def test_moov_after_mdat() -> None:
    info = parse_bytes(ftyp() + MDAT + moov(trak(b"vide")))
    assert info.moov_before_mdat is False


def test_edit_list_and_disabled_track_detected() -> None:
    info = parse_bytes(ftyp() + moov(trak(b"vide", edit=True), trak(b"soun", enabled=False, track_id=2)) + MDAT)
    assert [t.handler for t in info.tracks_with_edit_lists] == ["vide"]
    assert [t.handler for t in info.disabled_tracks] == ["soun"]


def test_tkhd_version_1_track_id() -> None:
    data = ftyp() + box(b"moov", box(b"trak", tkhd(True, 7, version=1) + box(b"mdia", hdlr(b"vide")))) + MDAT
    assert parse_bytes(data).tracks[0].track_id == 7


def test_large_size_box_and_size_zero_box() -> None:
    data = ftyp() + box(b"moov", trak(b"vide"), large=True) + struct.pack(">I4s", 0, b"mdat") + b"\x00" * 32
    info = parse_bytes(data)
    assert info.top_level == ["ftyp", "moov", "mdat"]
    assert info.moov_before_mdat is True
    assert info.tracks[0].handler == "vide"
    assert not info.truncated


def qt_hdlr(component: bytes, subtype: bytes) -> bytes:
    """QuickTime handler: component type ('mhlr' media, 'dhlr' data) and subtype."""
    return full_box(b"hdlr", 0, 0, component + subtype + b"\x00" * 12 + b"\x00")


def test_quicktime_data_handler_does_not_replace_the_media_handler() -> None:
    def qt_trak(kind: bytes, track_id: int) -> bytes:
        minf = box(b"minf", qt_hdlr(b"dhlr", b"url ") + box(b"dinf", b"") + box(b"stbl", b""))
        return box(b"trak", tkhd(True, track_id) + box(b"edts", elst()) + box(b"mdia", qt_hdlr(b"mhlr", kind) + minf))

    info = parse_bytes(ftyp(b"qt  ") + moov(qt_trak(b"vide", 1), qt_trak(b"soun", 2)) + MDAT)
    assert [t.handler for t in info.tracks] == ["vide", "soun"]
    assert [t.handler for t in info.tracks_with_edit_lists] == ["vide", "soun"]


def test_truncated_file_is_flagged() -> None:
    data = ftyp() + moov(trak(b"vide")) + struct.pack(">I4s", 10_000, b"mdat") + b"\x00" * 10
    info = parse_bytes(data)
    assert info.truncated
    assert info.moov_before_mdat is True


def test_garbage_is_not_iso() -> None:
    info = parse_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 60)  # EBML (Matroska) magic
    assert not info.is_iso
    assert info.moov_before_mdat is None


def test_fragmented_mp4() -> None:
    data = ftyp() + moov(trak(b"vide")) + box(b"moof", b"") + MDAT + box(b"moof", b"") + MDAT
    info = parse_bytes(data)
    assert info.fragmented
    assert info.moov_before_mdat is True


def test_container_kind_from_brand() -> None:
    assert parse_bytes(ftyp(b"qt  ") + moov() + MDAT).container_kind() == "mov"
    assert parse_bytes(ftyp(b"M4V ") + moov() + MDAT).container_kind() == "m4v"
    assert parse_bytes(ftyp(b"3gp4") + moov() + MDAT).container_kind() == "3gp"
    assert parse_bytes(ftyp(b"mp42") + moov() + MDAT).container_kind() == "mp4"
    assert parse_bytes(moov() + MDAT).container_kind() == "mov"  # old QuickTime without ftyp


def test_corrupt_inner_box_does_not_crash() -> None:
    bad_trak = struct.pack(">I4s", 9999, b"trak") + b"\x00" * 8
    info = parse_bytes(ftyp() + box(b"moov", bad_trak) + MDAT)
    assert info.is_iso
    assert info.tracks == []


def test_deeply_nested_boxes_do_not_crash() -> None:
    inner = trak(b"vide")
    for _ in range(5000):
        inner = box(b"edts", inner)
    info = parse_bytes(ftyp() + box(b"moov", box(b"trak", inner)) + MDAT)
    assert info.is_iso and info.moov_before_mdat is True


def test_parse_file(tmp_path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(ftyp() + moov(trak(b"vide", edit=True)) + MDAT)
    info = parse_file(str(path))
    assert info.moov_before_mdat is True
    assert len(info.tracks_with_edit_lists) == 1
    assert info.to_dict()["tracks"][0]["edit_list_entries"] == 1
