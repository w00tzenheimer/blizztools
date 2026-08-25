"""
TACT archive index support.

Most CDN data is not stored as a loose `data/xx/yy/{ekey}` object. Files are
packed into a few dozen large archives, and each archive has a companion
`{archive}.index` listing the EKeys it contains along with their byte offset
and length. Fetching such a file means a ranged GET into the archive.

The `.index` layout is a sequence of fixed-size blocks followed by a 28-byte
footer that describes the field widths:

    entry  := key[key_size_bytes] size[size_bytes BE] offset[offset_bytes BE]
    block  := entry * (block_size_kb * 1024 // sizeof(entry))   # zero padded
    footer := toc_hash[8] version _11 _12 block_size_kb
              offset_bytes size_bytes key_size_bytes checksum_size
              num_elements[4 LE] footer_checksum[8]
"""

import struct
from typing import Dict, Tuple

ARCHIVE_INDEX_FOOTER_SIZE = 28

# (offset, size) of an entry within its archive
ArchiveLocation = Tuple[int, int]


class ArchiveIndexError(Exception):
    pass


def parse_archive_index(data: bytes) -> Dict[bytes, ArchiveLocation]:
    """
    Parse an archive `.index` into {ekey_bytes: (offset, size)}.

    Raises ArchiveIndexError if the footer is missing or self-inconsistent.
    """
    if len(data) < ARCHIVE_INDEX_FOOTER_SIZE:
        raise ArchiveIndexError(
            f"index too small: {len(data)} bytes, need at least "
            f"{ARCHIVE_INDEX_FOOTER_SIZE}"
        )

    footer = data[-ARCHIVE_INDEX_FOOTER_SIZE:]
    (
        _toc_hash,
        _version,
        _11,
        _12,
        block_size_kb,
        offset_bytes,
        size_bytes,
        key_size_bytes,
        _checksum_size,
        num_elements,
    ) = struct.unpack("<8sBBBBBBBBI", footer[:20])

    if not (key_size_bytes and size_bytes and offset_bytes and block_size_kb):
        raise ArchiveIndexError(
            f"implausible footer: key={key_size_bytes} size={size_bytes} "
            f"offset={offset_bytes} block_kb={block_size_kb}"
        )

    entry_size = key_size_bytes + size_bytes + offset_bytes
    block_size = block_size_kb * 1024
    entries_per_block = block_size // entry_size
    if entries_per_block == 0:
        raise ArchiveIndexError(
            f"entry size {entry_size} exceeds block size {block_size}"
        )

    result: Dict[bytes, ArchiveLocation] = {}
    pos = 0
    remaining = num_elements
    while remaining > 0:
        block = data[pos : pos + block_size]
        if not block:
            raise ArchiveIndexError(
                f"index truncated: {num_elements - remaining} of "
                f"{num_elements} entries read"
            )
        count = min(entries_per_block, remaining)
        for i in range(count):
            raw = block[i * entry_size : (i + 1) * entry_size]
            key = raw[:key_size_bytes]
            size = int.from_bytes(raw[key_size_bytes : key_size_bytes + size_bytes], "big")
            offset = int.from_bytes(raw[key_size_bytes + size_bytes :], "big")
            result[key] = (offset, size)
        remaining -= count
        pos += block_size

    return result


def parse_cdn_config(data: bytes) -> Dict[str, list]:
    """
    Parse a CDN config (same key-value text shape as a build config) into
    {key: [values]}. Unlike cdn.parse_build_config this is order-independent,
    because callers only want the `archives` / `file-index` lists.
    """
    out: Dict[str, list] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.split()
    return out
