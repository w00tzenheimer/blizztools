"""
Key material for TACT decryption.

Two independent key systems exist:

* **Armadillo keys** decrypt whole config/data objects on internal branches
  (wowdev). Each is a 16- or 32-byte Salsa20 key stored in an external
  `{name}.ak` file, followed by a 4-byte MD5(key)[:4] checksum. The key is
  never on the CDN or in a shipped client (verified by reversing the W3
  client's tact::ArmadilloKey::ReadArmadilloKey), so it must be supplied.

* **TACT content keys** decrypt individual BLTE 'E' chunks (embargoed retail
  content). Each is a 16-byte key addressed by an 8-byte key name; catalogs of
  known keys are published after content ships. `KeyCatalog` loads the common
  `<hex16-name> <hex32-key>` line format (WoWDBDefs / CASCLib TactKey.csv).
"""

import binascii
import hashlib
from pathlib import Path
from typing import Dict, Optional, Union

# .ak files: key bytes + MD5(key)[:4]. 20 bytes = 16-byte key, 36 = 32-byte key.
_AK_SIZES = {20: 16, 36: 32}


class ArmadilloKeyError(Exception):
    pass


def load_armadillo_key(source: Union[str, Path, bytes]) -> bytes:
    """
    Load and validate an Armadillo key.

    `source` may be a path to a `.ak` file, or the raw file bytes. Returns the
    16- or 32-byte key. A bare 16/32-byte key (no checksum) is accepted as-is;
    a full `.ak` blob has its trailing MD5(key)[:4] checksum verified, matching
    the client's tact::ArmadilloKey::ReadArmadilloKey.
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        path = Path(source)
        if not path.exists() and not str(source).endswith(".ak"):
            path = Path(str(source) + ".ak")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ArmadilloKeyError(f"cannot read Armadillo key {source}: {e}") from e

    # A bare key with no checksum.
    if len(data) in (16, 32):
        return data

    key_len = _AK_SIZES.get(len(data))
    if key_len is None:
        raise ArmadilloKeyError(
            f"unsupported Armadillo key length {len(data)} "
            f"(expected 16/32 raw, or 20/36 with checksum)"
        )
    key, checksum = data[:key_len], data[key_len : key_len + 4]
    if hashlib.md5(key).digest()[:4] != checksum:
        raise ArmadilloKeyError("Armadillo key checksum mismatch (broken .ak file)")
    return key


def _normalize_key_name(name: Union[int, str, bytes]) -> int:
    """A BLTE 'E' key name is a 64-bit id; accept int, hex string, or bytes."""
    if isinstance(name, int):
        return name
    if isinstance(name, (bytes, bytearray)):
        if len(name) != 8:
            raise ValueError(f"key name must be 8 bytes, got {len(name)}")
        # Key names are little-endian u64 on the wire (see blte 'E' parsing).
        return int.from_bytes(name, "little")
    return int(name, 16)


class KeyCatalog:
    """Lookup of BLTE 'E' content keys by 8-byte key name."""

    def __init__(self):
        self._keys: Dict[int, bytes] = {}

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, name: Union[int, str, bytes], key: Union[str, bytes]) -> None:
        key_bytes = binascii.unhexlify(key) if isinstance(key, str) else bytes(key)
        if len(key_bytes) != 16:
            raise ValueError(f"TACT key must be 16 bytes, got {len(key_bytes)}")
        self._keys[_normalize_key_name(name)] = key_bytes

    def get(self, name: Union[int, str, bytes]) -> Optional[bytes]:
        return self._keys.get(_normalize_key_name(name))

    def load_file(self, path: Union[str, Path]) -> int:
        """
        Load a `<name-hex16> <key-hex32>` catalog (whitespace/comma separated).

        Blank lines and lines starting with '#' are ignored. Returns the count
        of keys added.
        """
        added = 0
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 2:
                continue
            name, key = parts[0], parts[1]
            if len(name) != 16 or len(key) != 32:
                continue
            try:
                self.add(name, key)
                added += 1
            except (binascii.Error, ValueError):
                continue
        return added
