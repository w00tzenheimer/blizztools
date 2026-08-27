"""
TACT decryption entry points, built on the Salsa20 core.

Two distinct schemes, both Salsa20/20:

* **Armadillo** (`armadillo_decrypt`) decrypts a whole config/data object on an
  internal branch. Key = the 16/32-byte .ak key; nonce = bytes [8:16) of the
  object's own content hash; block counter starts at 0 for a whole object.

* **BLTE 'E' chunk** (`decrypt_e_chunk`) decrypts one embargoed chunk inside a
  normal container. The chunk header carries an 8-byte key name and a 4-byte
  IV; the chunk's index within the container is XORed into the low IV bytes.

Wire formats cross-checked against BuildBackup's BLTE.cs and the Warcraft III
client's tact::ArmadilloCoder / CoderCrypt paths.
"""

from typing import Optional, Union

from .keys import KeyCatalog
from .salsa20 import crypt

SALSA20_BLOCK = 64


def _name_bytes(object_name: Union[str, bytes]) -> bytes:
    if isinstance(object_name, (bytes, bytearray)):
        return bytes(object_name)
    return bytes.fromhex(object_name)


def armadillo_decrypt(
    data: bytes, key: bytes, object_name: Union[str, bytes], offset: int = 0
) -> bytes:
    """
    Decrypt an Armadillo-encrypted object.

    `object_name` is the object's content hash (the hex the CDN addresses it
    by, e.g. the build-config hash); its bytes [8:16) form the Salsa20 nonce.
    `offset` is the object offset of `data`'s first byte (0 for a whole file),
    used to seed the block counter for random-access decryption.
    """
    name = _name_bytes(object_name)
    if len(name) < 16:
        raise ValueError(f"object name must be >= 16 bytes, got {len(name)}")
    nonce = name[8:16]
    if offset % SALSA20_BLOCK != 0:
        raise ValueError("offset must be a multiple of 64 for block-aligned decryption")
    return crypt(key, nonce, data, counter=offset // SALSA20_BLOCK)


class MissingKeyError(Exception):
    """Raised when a BLTE 'E' chunk names a key the catalog does not have."""

    def __init__(self, key_name: int):
        self.key_name = key_name
        super().__init__(f"unknown BLTE encryption key name {key_name:016X}")


def decrypt_e_chunk(
    data: bytes, block_index: int, catalog: Optional[KeyCatalog]
) -> bytes:
    """
    Decrypt one BLTE 'E' chunk, returning the inner block (starting with its
    own N/Z/F mode byte, to be processed as usual).

    `data` is the whole chunk including the leading 'E' byte. `block_index` is
    the chunk's position in the container. Raises MissingKeyError if the key is
    unavailable, NotImplementedError for the ARC4 ('A') variant.
    """
    if not data or data[0:1] != b"E":
        raise ValueError("not an 'E' (encrypted) BLTE chunk")

    key_name_size = data[1]
    if key_name_size != 8:
        raise ValueError(f"unexpected key-name size {key_name_size} (expected 8)")
    key_name = int.from_bytes(data[2 : 2 + key_name_size], "little")

    iv_size = data[2 + key_name_size]
    if not 0 < iv_size <= 0x10:
        raise ValueError(f"unexpected IV size {iv_size}")
    iv_part = data[3 + key_name_size : 3 + key_name_size + iv_size]

    enc_type_pos = 3 + key_name_size + iv_size
    enc_type = data[enc_type_pos : enc_type_pos + 1]
    payload = data[enc_type_pos + 1 :]

    key = catalog.get(key_name) if catalog is not None else None
    if key is None:
        raise MissingKeyError(key_name)

    # Nonce: the 4-byte IV, zero-extended to 8, with the block index folded
    # into the low bytes so each chunk keys a distinct stream.
    nonce = bytearray(8)
    nonce[: len(iv_part)] = iv_part
    for i in range(4):
        nonce[i] ^= (block_index >> (8 * i)) & 0xFF

    if enc_type == b"S":  # Salsa20
        return crypt(key, bytes(nonce), payload, counter=0)
    if enc_type == b"A":  # ARC4
        raise NotImplementedError("BLTE 'E' ARC4 ('A') encryption is not supported")
    raise ValueError(f"unknown BLTE encryption type {enc_type!r}")
