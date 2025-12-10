import binascii

from construct import (
    Adapter,
    Array,
    Bytes,
    Const,
    Enum,
    GreedyBytes,
    GreedyRange,
    Int8ub,
    Int16ub,
    Int32ub,
    NullTerminated,
    Struct,
    this,
)


class Latin1CStringAdapter(Adapter):
    def __init__(self):
        super().__init__(NullTerminated(GreedyBytes))

    def _decode(self, obj, context, path):
        return obj.decode("latin-1")

    def _encode(self, obj, context, path):
        return obj.encode("latin-1")


def Latin1CString():
    return Latin1CStringAdapter()


class Md5Hash:
    def __init__(self, data):
        if isinstance(data, str):
            if len(data) != 32:
                raise ValueError("MD5 hash string must be 32 characters long")
            self.data = binascii.unhexlify(data)
        elif isinstance(data, bytes):
            if len(data) != 16:
                raise ValueError("MD5 hash bytes must be 16 bytes long")
            self.data = data
        else:
            raise TypeError(f"Unsupported type for Md5Hash: {type(data)}")

    def __str__(self):
        return binascii.hexlify(self.data).decode("ascii")

    def __repr__(self):
        return f"Md5Hash('{self}')"

    def __eq__(self, other):
        if isinstance(other, Md5Hash):
            return self.data == other.data
        return False

    def __hash__(self):
        return hash(self.data)

    def is_null(self):
        return self.data == b"\x00" * 16


class Md5HashConstructAdapter(Adapter):
    def _decode(self, obj, context, path):
        return Md5Hash(obj)

    def _encode(self, obj, context, path):
        return obj.data


Md5HashConstruct = Md5HashConstructAdapter(Bytes(16))

# from blte.rs

EncodingMode = Enum(
    Bytes(1),
    PlainData=b"N",
    Zlib=b"Z",
    Recursive=b"F",
    Encrypted=b"E",
)


def DataChunk(compressed_size):
    return Struct(
        "encoding_mode" / EncodingMode,
        "data" / Bytes(compressed_size - 1),
    )


ChunkInfoEntry = Struct(
    "compressed_size" / Int32ub,
    "decompressed_size" / Int32ub,
    "checksum" / Bytes(16),
)

# from lib.rs

InstallManifestEntry = Struct(
    "name" / Latin1CString(),
    "hash" / Md5HashConstruct,
    "size" / Int32ub,
)

ManifestTag = Struct(
    "name" / Latin1CString(),
    "tag_type" / Int16ub,
    "mask" / Bytes(this._.num_entries // 8),
)

InstallManifest = Struct(
    "magic" / Const(b"IN"),
    "version" / Int8ub,
    "encoding_hash_size" / Int8ub,
    "num_tags" / Int16ub,
    "num_entries" / Int32ub,
    "tags" / Array(this.num_tags, ManifestTag),
    "entries" / Array(this.num_entries, InstallManifestEntry),
)

DownloadManifestEntry = Struct(
    "hash" / Md5HashConstruct,
    "file_size" / Bytes(5),  # u40?
    "priority" / Int8ub,
)

DownloadManifest = Struct(
    "magic" / Const(b"DL"),
    "version" / Int8ub,
    "encoding_key_size" / Int8ub,
    "include_checksum" / Int8ub,
    "num_entries" / Int32ub,
    "num_tags" / Int16ub,
    "entries" / Array(this.num_entries, DownloadManifestEntry),
    "tags" / Array(this.num_tags, ManifestTag),
)

CeKeyTableIndex = Struct(
    "first_key" / Md5HashConstruct,
    "md5" / Md5HashConstruct,
)

CeKeyPageEntry = Struct(
    "key_count" / Int8ub,
    "file_size" / Bytes(5),
    "c_key" / Md5HashConstruct,
    "e_keys" / Array(this.key_count, Md5HashConstruct),
)

EncodingManifest = Struct(
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

IndexEntry = Struct(
    "e_key" / Md5HashConstruct,
    "size" / Int32ub,
    "offset" / Int32ub,
)

IndexFile = GreedyRange(IndexEntry)
