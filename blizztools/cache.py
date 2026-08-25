"""
Local disk cache for immutable CDN objects.

Archive `.index` files are addressed by the hash of their contents, so a given
hash always names the same bytes. That makes them safe to cache forever: the
only invalidation needed is eviction for space. Caching them matters because a
cold WoW run fetches 1,347 of them before it can resolve a single archived
file, and that is most of the wall clock.

The cache lives on local disk, deliberately not under the download destination,
which is often a slow network share.
"""

import os
import shutil
from pathlib import Path
from typing import Optional


def default_cache_dir() -> Path:
    """Cache root, honouring XDG_CACHE_HOME then falling back to ~/.cache."""
    env = os.environ.get("BLIZZTOOLS_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "blizztools"


class IndexCache:
    """
    Content-addressed store for archive index blobs.

    Reads and writes are best-effort: any filesystem problem degrades to a
    cache miss rather than failing the download.
    """

    def __init__(self, root: Optional[Path] = None, enabled: bool = True):
        self.root = Path(root) if root is not None else default_cache_dir()
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        # Shard by prefix so one directory does not accumulate thousands of
        # entries, mirroring the CDN's own layout.
        return self.root / "index" / key[:2] / f"{key}.index"

    def get(self, key: str) -> Optional[bytes]:
        if not self.enabled:
            return None
        try:
            data = self._path(key).read_bytes()
        except (OSError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, key: str, data: bytes) -> None:
        if not self.enabled:
            return
        target = self._path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temp file in the same directory so a concurrent
            # reader never observes a half-written index.
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError:
            try:
                tmp.unlink()
            except (OSError, UnboundLocalError, NameError):
                pass

    def clear(self) -> None:
        try:
            shutil.rmtree(self.root / "index")
        except OSError:
            pass

    @property
    def summary(self) -> str:
        total = self.hits + self.misses
        if not total:
            return "index cache unused"
        return f"index cache {self.hits}/{total} hits"
