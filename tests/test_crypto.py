import hashlib
import io
import os
import struct
import zlib

import pytest

from blizztools.blte import decode_blte_stream, parse_blte
from blizztools.keys import ArmadilloKeyError, KeyCatalog, load_armadillo_key
from blizztools.salsa20 import crypt, keystream
from blizztools.tact_crypt import MissingKeyError, armadillo_decrypt, decrypt_e_chunk


# --- Salsa20 against DJB's published test vectors -----------------------------

def test_salsa20_128bit_vector():
    ks = keystream(bytes([0x80] + [0] * 15), bytes(8), 64, 0)
    assert ks == bytes.fromhex(
        "4DFA5E481DA23EA09A31022050859936DA52FCEE218005164F267CB65F5CFD7F"
        "2B4F97E0FF16924A52DF269515110A07F9E460BC65EF95DA58F740B7D1DBB0AA"
    )


def test_salsa20_256bit_vector():
    ks = keystream(bytes([0x80] + [0] * 31), bytes(8), 64, 0)
    assert ks == bytes.fromhex(
        "E3BE8FDD8BECA2E3EA8EF9475B29A6E7003951E1097A5C38D23B7A5FAD9F6844"
        "B22C97559E2723C7CBBD3FE4FC8D9A0744652A83E72A9C461876AF4D7EF1A117"
    )


def test_salsa20_symmetric_and_counter_offset():
    k, n, msg = os.urandom(16), os.urandom(8), os.urandom(1000)
    assert crypt(k, n, crypt(k, n, msg)) == msg
    full = keystream(k, n, 192, 0)
    assert keystream(k, n, 64, 2) == full[128:192]


def test_salsa20_rejects_bad_sizes():
    with pytest.raises(ValueError):
        keystream(os.urandom(20), bytes(8), 16)
    with pytest.raises(ValueError):
        keystream(os.urandom(16), bytes(7), 16)


# --- .ak Armadillo key loading ------------------------------------------------

def test_load_bare_key():
    k = os.urandom(16)
    assert load_armadillo_key(k) == k
    k32 = os.urandom(32)
    assert load_armadillo_key(k32) == k32


def test_load_ak_with_checksum():
    k = os.urandom(16)
    ak = k + hashlib.md5(k).digest()[:4]
    assert load_armadillo_key(ak) == k


def test_load_ak_rejects_bad_checksum():
    k = os.urandom(16)
    with pytest.raises(ArmadilloKeyError):
        load_armadillo_key(k + b"\x00\x00\x00\x00")


def test_load_ak_rejects_bad_length():
    with pytest.raises(ArmadilloKeyError):
        load_armadillo_key(os.urandom(19))


def test_load_ak_from_file(tmp_path):
    k = os.urandom(16)
    f = tmp_path / "mykey.ak"
    f.write_bytes(k + hashlib.md5(k).digest()[:4])
    assert load_armadillo_key(str(f)) == k
    # extension-less path resolves to the .ak file
    assert load_armadillo_key(str(tmp_path / "mykey")) == k


# --- Armadillo config decryption (mirrors BuildBackup DecryptFile) ------------

def test_armadillo_roundtrip():
    key = os.urandom(16)
    name = os.urandom(16)
    plaintext = b"# Build Configuration\nroot = " + os.urandom(300)
    ciphertext = crypt(key, name[8:16], plaintext)  # IV = name[8:16]
    assert armadillo_decrypt(ciphertext, key, name.hex()) == plaintext


def test_armadillo_accepts_bytes_or_hex_name():
    key, name = os.urandom(16), os.urandom(16)
    ct = crypt(key, name[8:16], b"# config data here")
    assert armadillo_decrypt(ct, key, name) == armadillo_decrypt(ct, key, name.hex())


def test_armadillo_32byte_key():
    key, name = os.urandom(32), os.urandom(16)
    pt = b"# config " + os.urandom(100)
    assert armadillo_decrypt(crypt(key, name[8:16], pt), key, name) == pt


# --- BLTE 'E' chunk decryption (mirrors BuildBackup Decrypt) ------------------

def _catalog():
    cat = KeyCatalog()
    cat.add((0x1234567890ABCDEF).to_bytes(8, "little"), os.urandom(16))
    return cat


def _e_chunk(inner_block, block_index, cat):
    name = next(iter(cat._keys))
    key = cat._keys[name]
    iv_part = os.urandom(4)
    nonce = bytearray(8)
    nonce[:4] = iv_part
    for i in range(4):
        nonce[i] ^= (block_index >> (8 * i)) & 0xFF
    enc = crypt(key, bytes(nonce), inner_block)
    return b"E" + bytes([8]) + name.to_bytes(8, "little") + bytes([4]) + iv_part + b"S" + enc


def _blte(chunks):
    hdr = b"BLTE" + struct.pack(">I", 12 + 24 * len(chunks))
    hdr += bytes([0x0F, 0]) + struct.pack(">H", len(chunks))
    for c in chunks:
        hdr += struct.pack(">I", len(c)) + struct.pack(">I", 0) + b"\x00" * 16
    return hdr + b"".join(chunks)


def test_decrypt_e_chunk_direct():
    cat = _catalog()
    inner = b"N" + b"secret payload"
    chunk = _e_chunk(inner, 3, cat)
    assert decrypt_e_chunk(chunk, 3, cat) == inner


def test_blte_e_chunks_buffered_and_stream():
    cat = _catalog()
    chunks = [
        _e_chunk(b"N" + b"hello encrypted", 0, cat),
        _e_chunk(b"Z" + zlib.compress(b"C" * 5000), 1, cat),
    ]
    raw = _blte(chunks)
    expected = b"hello encrypted" + b"C" * 5000
    assert parse_blte(raw, keys=cat) == expected
    dst = io.BytesIO()
    decode_blte_stream(io.BytesIO(raw), dst, keys=cat)
    assert dst.getvalue() == expected


def test_blte_e_wrong_index_produces_garbage_not_plaintext():
    # The block index folds into the nonce, so decrypting a chunk at the wrong
    # index must NOT recover the plaintext (guards the index-XOR wiring).
    cat = _catalog()
    inner = b"N" + b"index-bound payload here"
    chunk = _e_chunk(inner, 5, cat)
    assert decrypt_e_chunk(chunk, 6, cat) != inner


def test_blte_missing_key_raises():
    cat = _catalog()
    raw = _blte([_e_chunk(b"N" + b"x", 0, cat)])
    with pytest.raises(MissingKeyError):
        parse_blte(raw, keys=KeyCatalog())
    with pytest.raises(MissingKeyError):
        parse_blte(raw, keys=None)


def test_blte_arc4_unsupported():
    cat = _catalog()
    name = next(iter(cat._keys))
    chunk = b"E" + bytes([8]) + name.to_bytes(8, "little") + bytes([4]) + os.urandom(4) + b"A" + b"xx"
    with pytest.raises(NotImplementedError):
        decrypt_e_chunk(chunk, 0, cat)


def test_plain_blte_still_works_without_keys():
    raw = _blte([b"N" + b"plain data"])
    assert parse_blte(raw) == b"plain data"


# --- KeyCatalog file loading --------------------------------------------------

def test_key_catalog_load_file(tmp_path):
    f = tmp_path / "keys.csv"
    name = "1234567890ABCDEF"
    key = "00112233445566778899AABBCCDDEEFF"
    f.write_text(f"# comment\n\n{name} {key}\ngarbage line\n{name.lower()},{key.lower()}\n")
    cat = KeyCatalog()
    n = cat.load_file(f)
    assert n == 2
    assert cat.get(name) == bytes.fromhex(key)
    # name lookup is endianness-consistent with the 8-byte wire form
    assert cat.get(int(name, 16)) == bytes.fromhex(key)
