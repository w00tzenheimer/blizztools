from io import BytesIO

from construct import Array, Bytes, Const, Int8ub, Int16ub, Int32ub, Struct, this

from .models import CeKeyPageEntry, CeKeyTableIndex


def parse_encoding_manifest(data: bytes):
    EncodingManifestHeader = Struct(
        "magic" / Const(b"EN"),
        "version" / Int8ub,
        "ckey_hash_size" / Int8ub,
        "ekey_hash_size" / Int8ub,
        "ce_page_size_kb" / Int16ub,
        "e_page_size_kb" / Int16ub,
        "ce_key_table_page_count" / Int32ub,
        "e_key_table_count" / Int32ub,
        "_unknown" / Int8ub,
        "espec_block_size" / Int32ub,
        "espec_block" / Bytes(this.espec_block_size),
        "ce_key_table_index" / Array(this.ce_key_table_page_count, CeKeyTableIndex),
    )

    stream = BytesIO(data)
    header = EncodingManifestHeader.parse_stream(stream)

    page_size = header.ce_page_size_kb * 1024
    num_pages = header.ce_key_table_page_count

    results = []
    for _ in range(num_pages):
        page_data = stream.read(page_size)
        if not page_data:
            break

        page_stream = BytesIO(page_data)
        while page_stream.tell() < len(page_data):
            try:
                entry = CeKeyPageEntry.parse_stream(page_stream)
                results.append(entry)
            except Exception as e:
                # Padding at the end of the page can cause parsing errors
                break

    return header, results
