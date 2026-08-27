"""
Salsa20 stream cipher (pure Python, no dependencies).

TACT uses Salsa20/20 in two places: BLTE 'E' chunk encryption (embargoed
content, keyed by an 8-byte key name) and whole-config "Armadillo" encryption
(internal branches, keyed by an external .ak file). Both need a keystream
generator with an explicit 64-byte block counter, which is what `keystream`
provides.

Verified against D. J. Bernstein's published Salsa20 test vectors in the test
suite. This is a clean-room implementation of the public specification, not
derived from any Blizzard code.
"""

import struct
from typing import List

_SIGMA = b"expand 32-byte k"  # 256-bit key
_TAU = b"expand 16-byte k"  # 128-bit key (what TACT's Armadillo uses)
_MASK = 0xFFFFFFFF


def _rotl(v: int, c: int) -> int:
    v &= _MASK
    return ((v << c) | (v >> (32 - c))) & _MASK


def _core(state: List[int]) -> bytes:
    """The Salsa20 hash: 20 rounds over a 16-word state, then add-back."""
    x = list(state)
    for _ in range(10):  # 10 double-rounds = 20 rounds
        # column round
        x[4] ^= _rotl(x[0] + x[12], 7)
        x[8] ^= _rotl(x[4] + x[0], 9)
        x[12] ^= _rotl(x[8] + x[4], 13)
        x[0] ^= _rotl(x[12] + x[8], 18)
        x[9] ^= _rotl(x[5] + x[1], 7)
        x[13] ^= _rotl(x[9] + x[5], 9)
        x[1] ^= _rotl(x[13] + x[9], 13)
        x[5] ^= _rotl(x[1] + x[13], 18)
        x[14] ^= _rotl(x[10] + x[6], 7)
        x[2] ^= _rotl(x[14] + x[10], 9)
        x[6] ^= _rotl(x[2] + x[14], 13)
        x[10] ^= _rotl(x[6] + x[2], 18)
        x[3] ^= _rotl(x[15] + x[11], 7)
        x[7] ^= _rotl(x[3] + x[15], 9)
        x[11] ^= _rotl(x[7] + x[3], 13)
        x[15] ^= _rotl(x[11] + x[7], 18)
        # row round
        x[1] ^= _rotl(x[0] + x[3], 7)
        x[2] ^= _rotl(x[1] + x[0], 9)
        x[3] ^= _rotl(x[2] + x[1], 13)
        x[0] ^= _rotl(x[3] + x[2], 18)
        x[6] ^= _rotl(x[5] + x[4], 7)
        x[7] ^= _rotl(x[6] + x[5], 9)
        x[4] ^= _rotl(x[7] + x[6], 13)
        x[5] ^= _rotl(x[4] + x[7], 18)
        x[11] ^= _rotl(x[10] + x[9], 7)
        x[8] ^= _rotl(x[11] + x[10], 9)
        x[9] ^= _rotl(x[8] + x[11], 13)
        x[10] ^= _rotl(x[9] + x[8], 18)
        x[12] ^= _rotl(x[15] + x[14], 7)
        x[13] ^= _rotl(x[12] + x[15], 9)
        x[14] ^= _rotl(x[13] + x[12], 13)
        x[15] ^= _rotl(x[14] + x[13], 18)
    return struct.pack("<16I", *[(x[i] + state[i]) & _MASK for i in range(16)])


def _initial_state(key: bytes, nonce8: bytes, counter: int) -> List[int]:
    if len(key) == 32:
        const, k0, k1 = _SIGMA, key[:16], key[16:32]
    elif len(key) == 16:
        const, k0, k1 = _TAU, key[:16], key[:16]
    else:
        raise ValueError(f"Salsa20 key must be 16 or 32 bytes, got {len(key)}")
    if len(nonce8) != 8:
        raise ValueError(f"Salsa20 nonce must be 8 bytes, got {len(nonce8)}")

    c = struct.unpack("<4I", const)
    k0w = struct.unpack("<4I", k0)
    k1w = struct.unpack("<4I", k1)
    n = struct.unpack("<2I", nonce8)
    ctr_lo = counter & _MASK
    ctr_hi = (counter >> 32) & _MASK
    # Standard Salsa20 layout: constants on the diagonal, key around it,
    # 64-bit nonce and 64-bit block counter in the middle.
    return [
        c[0], k0w[0], k0w[1], k0w[2],
        k0w[3], c[1], n[0], n[1],
        ctr_lo, ctr_hi, c[2], k1w[0],
        k1w[1], k1w[2], k1w[3], c[3],
    ]


def keystream(key: bytes, nonce8: bytes, length: int, counter: int = 0) -> bytes:
    """Generate `length` keystream bytes from block `counter` onward."""
    out = bytearray()
    block = counter
    while len(out) < length:
        state = _initial_state(key, nonce8, block)
        out += _core(state)
        block += 1
    return bytes(out[:length])


def crypt(key: bytes, nonce8: bytes, data: bytes, counter: int = 0) -> bytes:
    """XOR `data` with the keystream. Salsa20 is symmetric: encrypt == decrypt."""
    ks = keystream(key, nonce8, len(data), counter)
    return bytes(a ^ b for a, b in zip(data, ks))
