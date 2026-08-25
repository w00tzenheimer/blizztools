from io import BytesIO

from construct import Array, Bytes, Const, Int8ub, Int16ub, Int32ub, Struct, this

from .models import CeKeyPageEntry, CeKeyTableIndex


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


def parse_encoding_manifest(data):
    """
    Parse an encoding manifest from bytes or a binary stream.

    WoW's encoding manifest is ~187 MB encoded and larger decoded, so callers
    are encouraged to pass an open file rather than a bytes object.
    """
    stream = BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
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


def iter_ce_entries(data):
    """
    Yield (ckey_bytes, first_ekey_bytes) one entry at a time.

    parse_encoding_manifest materializes every entry as a construct Container
    holding Md5Hash objects. WoW's manifest has ~2.87M of them, which costs
    gigabytes of RSS. Callers that only need a lookup should iterate here and
    keep just the keys they want.
    """
    stream = BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    header = EncodingManifestHeader.parse_stream(stream)

    page_size = header.ce_page_size_kb * 1024
    for _ in range(header.ce_key_table_page_count):
        page_data = stream.read(page_size)
        if not page_data:
            break
        page_stream = BytesIO(page_data)
        while page_stream.tell() < len(page_data):
            try:
                entry = CeKeyPageEntry.parse_stream(page_stream)
            except Exception:
                # Trailing zero padding within a page.
                break
            if entry.e_keys:
                yield entry.c_key.data, entry.e_keys[0].data
