import zlib

from construct import Array, Const, Int8ub, Int16ub, Int32ub, Struct

from .models import ChunkInfoEntry, DataChunk

BLTE_MAGIC = b"BLTE"


def parse_blte(data: bytes):
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

    chunks = []
    current_offset = chunk_data_offset
    for entry in chunk_info_entries:
        chunk_data_bytes = data[current_offset : current_offset + entry.compressed_size]
        chunk = DataChunk(entry.compressed_size).parse(chunk_data_bytes)
        chunks.append(chunk)
        current_offset += entry.compressed_size

    return decompress_chunks(chunks)


def decompress_chunks(chunks):
    full_data = bytearray()
    for chunk in chunks:
        if chunk.encoding_mode == "PlainData":
            full_data.extend(chunk.data)
        elif chunk.encoding_mode == "Zlib":
            full_data.extend(zlib.decompress(chunk.data))
        else:
            raise NotImplementedError(
                f"Encoding mode {chunk.encoding_mode} not supported"
            )
    return bytes(full_data)
