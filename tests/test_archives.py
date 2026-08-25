import struct

import pytest

from blizztools.archives import (
    ARCHIVE_INDEX_FOOTER_SIZE,
    ArchiveIndexError,
    parse_archive_index,
    parse_cdn_config,
)


def build_index(entries, block_size_kb=4, key_size=16, size_bytes=4, offset_bytes=4):
    """Build a synthetic archive .index matching the real on-wire layout."""
    entry_size = key_size + size_bytes + offset_bytes
    block_size = block_size_kb * 1024
    per_block = block_size // entry_size

    blob = b""
    for i in range(0, len(entries), per_block):
        chunk = entries[i : i + per_block]
        block = b""
        for key, offset, size in chunk:
            block += (
                key
                + size.to_bytes(size_bytes, "big")
                + offset.to_bytes(offset_bytes, "big")
            )
        blob += block.ljust(block_size, b"\x00")

    footer = struct.pack(
        "<8sBBBBBBBBI",
        b"\x00" * 8,
        1,
        0,
        0,
        block_size_kb,
        offset_bytes,
        size_bytes,
        key_size,
        8,
        len(entries),
    ) + b"\x00" * 8
    assert len(footer) == ARCHIVE_INDEX_FOOTER_SIZE
    return blob + footer


def test_parse_archive_index_single_entry():
    key = bytes(range(16))
    data = build_index([(key, 222624397, 1547708)])
    parsed = parse_archive_index(data)
    assert parsed == {key: (222624397, 1547708)}


def test_parse_archive_index_spans_multiple_blocks():
    # 4KB block / 24-byte entry = 170 per block; 200 forces a second block.
    entries = [
        (bytes([i // 256, i % 256]) + b"\x00" * 14, i * 100, i + 1) for i in range(200)
    ]
    parsed = parse_archive_index(build_index(entries))
    assert len(parsed) == 200
    for key, offset, size in entries:
        assert parsed[key] == (offset, size)


def test_parse_archive_index_ignores_block_padding():
    entries = [(bytes([i]) + b"\x00" * 15, i, i) for i in range(3)]
    parsed = parse_archive_index(build_index(entries))
    # Padding must not become a bogus all-zero entry beyond the real one.
    assert len(parsed) == 3


def test_parse_archive_index_rejects_short_input():
    with pytest.raises(ArchiveIndexError):
        parse_archive_index(b"\x00" * 10)


def test_parse_archive_index_rejects_zero_field_widths():
    footer = struct.pack(
        "<8sBBBBBBBBI", b"\x00" * 8, 1, 0, 0, 0, 0, 0, 0, 8, 0
    ) + b"\x00" * 8
    with pytest.raises(ArchiveIndexError):
        parse_archive_index(footer)


def test_parse_archive_index_detects_truncation():
    key = bytes(range(16))
    data = build_index([(key, 1, 2)])
    # Claim 500 entries but supply one block's worth of data.
    tampered = bytearray(data)
    struct.pack_into("<I", tampered, len(data) - ARCHIVE_INDEX_FOOTER_SIZE + 16, 500)
    with pytest.raises(ArchiveIndexError):
        parse_archive_index(bytes(tampered))


def test_parse_cdn_config_lists_and_comments():
    raw = b"# comment\n\narchives = aaa bbb ccc\nfile-index = ddd\nbad line\n"
    cfg = parse_cdn_config(raw)
    assert cfg["archives"] == ["aaa", "bbb", "ccc"]
    assert cfg["file-index"] == ["ddd"]
    assert "bad line" not in cfg
