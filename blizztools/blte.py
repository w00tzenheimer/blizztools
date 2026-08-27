import zlib

from construct import Array, Const, Int8ub, Int16ub, Int32ub, Struct

from .models import ChunkInfoEntry, DataChunk

BLTE_MAGIC = b"BLTE"


def parse_blte(data: bytes, keys=None):
    if data[:4] != BLTE_MAGIC:
        raise ValueError("Invalid BLTE data: magic mismatch")

    header_size = int.from_bytes(data[4:8], "big")

    if header_size == 0:
        # No header, just raw chunks
        chunk_count = 1
        # Fake a single chunk entry
        chunk_info_entries = [
            Struct(
                "compressed_size" / Const(len(data), Int32ub),
                "decompressed_size" / Const(0, Int32ub),
                "checksum" / Const(b"\x00" * 16, Bytes(16)),
            ).parse(b"")
        ]
        chunk_data_offset = 0
    else:
        # Has a header
        chunk_info_format = Struct(
            "flags" / Int8ub,
            "flag_ext" / Int8ub,
            "chunk_count" / Int16ub,
        )
        chunk_info = chunk_info_format.parse(data[8:12])
        chunk_count = chunk_info.chunk_count

        chunk_info_entries = Array(chunk_count, ChunkInfoEntry).parse(data[12:])
        chunk_data_offset = 12 + chunk_count * (4 + 4 + 16)  # sizeof(ChunkInfoEntry)

    raw_chunks = []
    current_offset = chunk_data_offset
    for entry in chunk_info_entries:
        chunk_data_bytes = data[current_offset : current_offset + entry.compressed_size]
        raw_chunks.append(chunk_data_bytes)
        current_offset += entry.compressed_size

    return decompress_chunks(raw_chunks, keys=keys)


def _decode_block(block: bytes, index: int, keys):
    """Decode one BLTE block (mode byte + payload) to plaintext bytes."""
    mode = block[0:1]
    if mode == b"N":
        return block[1:]
    if mode == b"Z":
        return zlib.decompress(block[1:])
    if mode == b"E":
        from .tact_crypt import decrypt_e_chunk

        # Decrypting yields another block (N/Z/...) to decode in turn.
        inner = decrypt_e_chunk(block, index, keys)
        return _decode_block(inner, index, keys)
    if mode == b"F":
        raise NotImplementedError("Recursive BLTE frames ('F') are not supported")
    raise NotImplementedError(f"Encoding mode {mode!r} not supported")


def decompress_chunks(raw_chunks, keys=None):
    """
    Decode raw BLTE chunks (each: 1 mode byte + payload) to bytes.

    `keys` is an optional KeyCatalog used to decrypt 'E' chunks; without it an
    encrypted chunk raises MissingKeyError.
    """
    full_data = bytearray()
    for index, chunk in enumerate(raw_chunks):
        # Accept either raw bytes or a legacy construct-parsed chunk object.
        if not isinstance(chunk, (bytes, bytearray)):
            mode = {"PlainData": b"N", "Zlib": b"Z", "Encrypted": b"E", "Recursive": b"F"}
            chunk = mode.get(str(chunk.encoding_mode), b"?") + bytes(chunk.data)
        full_data.extend(_decode_block(bytes(chunk), index, keys))
    return bytes(full_data)


# Streaming decode -------------------------------------------------------------
#
# parse_blte() holds the encoded body, every decoded chunk, and the joined
# result in memory at once. For a 130 MB Mac binary or WoW's 187 MB encoding
# manifest that is several copies of a large file. The functions below walk the
# chunk table and decode one chunk at a time straight to an output stream, so
# peak memory is one chunk plus the zlib window.

STREAM_BLOCK = 1 << 20

CHUNK_INFO_ENTRY_SIZE = 4 + 4 + 16


def _copy_plain(src, dst, remaining: int) -> int:
    written = 0
    while remaining > 0:
        block = src.read(min(STREAM_BLOCK, remaining))
        if not block:
            raise ValueError("BLTE chunk truncated")
        dst.write(block)
        remaining -= len(block)
        written += len(block)
    return written


def _copy_zlib(src, dst, remaining: int) -> int:
    decompressor = zlib.decompressobj()
    written = 0
    while remaining > 0:
        block = src.read(min(STREAM_BLOCK, remaining))
        if not block:
            raise ValueError("BLTE chunk truncated")
        remaining -= len(block)
        out = decompressor.decompress(block)
        if out:
            dst.write(out)
            written += len(out)
    tail = decompressor.flush()
    if tail:
        dst.write(tail)
        written += len(tail)
    return written


def decode_blte_stream(src, dst, keys=None) -> int:
    """
    Decode a BLTE stream from `src` into `dst`, returning bytes written.

    `src` and `dst` are binary file objects; neither is fully materialized for
    plain/zlib chunks. `keys` is an optional KeyCatalog for 'E' chunks; an
    encrypted chunk is read whole (one chunk in memory), decrypted, and its
    inner block decoded.
    """
    if src.read(4) != BLTE_MAGIC:
        raise ValueError("Invalid BLTE data: magic mismatch")

    header_size = int.from_bytes(src.read(4), "big")

    if header_size == 0:
        # Headerless: a single chunk running to end of stream.
        mode = src.read(1)
        if mode == b"N":
            written = 0
            while True:
                block = src.read(STREAM_BLOCK)
                if not block:
                    return written
                dst.write(block)
                written += len(block)
        if mode == b"Z":
            decompressor = zlib.decompressobj()
            written = 0
            while True:
                block = src.read(STREAM_BLOCK)
                if not block:
                    tail = decompressor.flush()
                    dst.write(tail)
                    return written + len(tail)
                out = decompressor.decompress(block)
                dst.write(out)
                written += len(out)
        raise NotImplementedError(f"Encoding mode {mode!r} not supported")

    flags = src.read(4)
    chunk_count = int.from_bytes(flags[2:4], "big")

    sizes = []
    for _ in range(chunk_count):
        raw = src.read(CHUNK_INFO_ENTRY_SIZE)
        if len(raw) < CHUNK_INFO_ENTRY_SIZE:
            raise ValueError("BLTE chunk table truncated")
        sizes.append(int.from_bytes(raw[0:4], "big"))

    written = 0
    for index, compressed_size in enumerate(sizes):
        mode = src.read(1)
        remaining = compressed_size - 1
        if mode == b"N":
            written += _copy_plain(src, dst, remaining)
        elif mode == b"Z":
            written += _copy_zlib(src, dst, remaining)
        elif mode == b"E":
            # Encrypted: read the whole chunk, decrypt, decode the inner block.
            body = src.read(remaining)
            if len(body) < remaining:
                raise ValueError("BLTE chunk truncated")
            out = _decode_block(b"E" + body, index, keys)
            dst.write(out)
            written += len(out)
        else:
            raise NotImplementedError(
                f"Encoding mode {mode!r} not supported"
            )
    return written
