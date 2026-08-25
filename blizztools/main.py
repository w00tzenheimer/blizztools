import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import click
import httpx
from rich.console import Console

from blizztools.blte import STREAM_BLOCK as BLTE_STREAM_BLOCK
from blizztools.blte import decode_blte_stream, parse_blte
from blizztools.cdn import parse_build_config
from blizztools.encoding import iter_ce_entries, parse_encoding_manifest
from blizztools.models import InstallManifest, Md5Hash
from blizztools.archives import (
    ArchiveIndexError,
    parse_archive_index,
    parse_cdn_config,
)
from blizztools.cache import IndexCache
from blizztools.parsers import parse_cdn_table, parse_version_table
from blizztools.tags import build_variant_names
from blizztools.products import (
    ALL_PRODUCT_CODES,
    DEFAULT_PRODUCTS,
    PRODUCT_NAME_MAP,
    Product,
    _code_to_cli_name,
    product_name_to_enum,
)

console = Console()

CKEY_MAP_FILENAME = ".ckey_map.json"

BASE_URL = "http://us.patch.battle.net:1119"

# Blizzard's edges (level3.blizzard.com in particular) reset or truncate large
# HTTP/2 response bodies: a 268 MB object reset immediately and repeatably with
# StreamReset(INTERNAL_ERROR), and a 6 MiB range came back short as
# InvalidBodyLengthError, while HTTP/1.1 streamed the same bytes reliably.
# Data transfers dominate here, so HTTP/1.1 is the safer default.
USE_HTTP2 = False


def should_download(filename: str, patterns: Iterable[re.Pattern]) -> bool:
    """Check if filename matches any of the patterns."""
    return any(p.search(filename) for p in patterns)


# Extensions treated as the compiled artifact a .pdb provides symbols for.
COMPANION_EXTENSIONS = frozenset(
    {".dll", ".exe", ".sys", ".ocx", ".node", ".so", ".dylib"}
)


def _basename_stem(name: str) -> Tuple[str, str]:
    """Split a manifest entry name into (basename stem, lowercased extension)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return base, ""
    return base[:dot], base[dot:].lower()


# Trailing build stamp on symbol files, e.g. 'Wow_11.1.0.60257' -> 'Wow'.
_VERSION_SUFFIX_RE = re.compile(r"[_-]\d+(?:\.\d+){1,4}$")


def find_pdb_companions(
    pdb_name: str, entries_by_stem: Dict[str, List[str]]
) -> List[str]:
    """
    Find manifest entry names that look like the binary a .pdb belongs to.

    Matching is on basename stem across the whole manifest, not directory:
    Blizzard ships e.g. 'Hearthstone_Data/Plugins/x86_64/NgWebview.pdb'
    alongside 'NgWebview.dll' at the manifest root, so a same-directory
    lookup finds nothing.

    Falls back to stripping a trailing build stamp, since some symbol files
    are version-named ('Wow_11.1.0.60257.pdb') while the binary is not
    ('Wow.exe'). The exact stem always wins when it matches something.

    This is a filename heuristic. The authoritative pairing is the PDB
    GUID/age in the binary's debug directory, which is not knowable from
    manifest names alone.
    """
    stem, ext = _basename_stem(pdb_name)
    if ext != ".pdb":
        return []

    exact = entries_by_stem.get(stem.lower())
    if exact:
        return list(exact)

    trimmed = _VERSION_SUFFIX_RE.sub("", stem)
    if trimmed != stem:
        return list(entries_by_stem.get(trimmed.lower(), []))
    return []


def select_grab_entries(entries, patterns: Iterable[re.Pattern], with_companions: bool):
    """
    Choose which manifest entries to download.

    Returns a list of (index, entry, is_companion) in manifest order, where
    index is the entry's position in the manifest (needed to look up its
    tags). Entries matching a caller pattern come through with is_companion
    False; binaries pulled in because a matched .pdb names them come through
    True. An entry that matches a pattern on its own is never demoted to a
    companion.
    """
    named = [(i, e) for i, e in enumerate(entries) if e.name]
    matched = {e.name for _, e in named if should_download(e.name, patterns)}

    companions = set()
    if with_companions:
        entries_by_stem: Dict[str, List[str]] = {}
        for _, e in named:
            stem, ext = _basename_stem(e.name)
            if ext in COMPANION_EXTENSIONS:
                entries_by_stem.setdefault(stem.lower(), []).append(e.name)

        for name in matched:
            for companion in find_pdb_companions(name, entries_by_stem):
                if companion not in matched:
                    companions.add(companion)

    return [
        (i, e, e.name in companions)
        for i, e in named
        if e.name in matched or e.name in companions
    ]


def make_unique_filename(
    base_path: Path, ckey: str, dir_index: "Optional[DirIndex]" = None
) -> Path:
    """
    Make a filename unique by appending CKey if the file already exists.
    Inserts CKey before the extension if there is one, otherwise appends it.
    If the resulting filename also exists, appends the full CKey.
    """
    exists = (
        dir_index.contains if dir_index is not None else lambda p: p.exists()
    )
    if not exists(base_path):
        return base_path

    # File exists, need to make it unique
    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent

    # Use first 8 characters of CKey for uniqueness (shorter, cleaner)
    ckey_short = ckey[:8]

    if suffix:
        # Has extension: insert CKey before extension
        new_name = f"{stem}.{ckey_short}{suffix}"
    else:
        # No extension: append CKey
        new_name = f"{stem}.{ckey_short}"

    unique_path = parent / new_name

    # If the unique filename also exists (very unlikely), use full CKey
    if exists(unique_path):
        if suffix:
            new_name = f"{stem}.{ckey}{suffix}"
        else:
            new_name = f"{stem}.{ckey}"
        unique_path = parent / new_name

    return unique_path


def link_duplicate(source: Path, target: Path) -> bool:
    """
    Materialize `target` from `source`, which holds identical content.

    Prefers a hard link so a file listed at several manifest paths costs disk
    once, and falls back to a copy when linking is unavailable (separate
    filesystems, or a share that does not support it). Returns False if
    neither worked, leaving the caller to download normally.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    try:
        os.link(source, target)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass

    try:
        shutil.copy2(source, target)
        return True
    except OSError:
        return False


def load_ckey_map(dest_dir: Path) -> Dict[str, Dict[str, str]]:
    """
    Load the CKey mapping from the destination directory.
    Returns a dictionary mapping CKey to file info (filename, product, version).
    """
    map_file = dest_dir / CKEY_MAP_FILENAME
    if not map_file.exists():
        return {}

    try:
        with open(map_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        # If file is corrupted, return empty dict
        return {}


def save_ckey_map(dest_dir: Path, ckey_map: Dict[str, Dict[str, str]]) -> None:
    """Save the CKey mapping to the destination directory."""
    map_file = dest_dir / CKEY_MAP_FILENAME
    try:
        with open(map_file, "w", encoding="utf-8") as f:
            json.dump(ckey_map, f, indent=2, sort_keys=True)
    except IOError as e:
        console.print(f"[yellow]⚠  Warning: Could not save CKey map: {e}[/yellow]")


class DirIndex:
    """
    Cached directory listings for existence probes.

    `grab` asks "does this file already exist?" once per manifest entry, and
    the collision check needs the whole directory listing. Done naively that
    is one listing plus a stat per entry, per file -- O(dir size) syscalls per
    file against a destination that is often a network share. Listing each
    directory once and answering from memory makes it O(dir size) per
    directory instead.

    Entries created during the run are registered so the cache stays truthful.
    """

    def __init__(self):
        self._dirs: Dict[str, set] = {}

    def names(self, directory: Path) -> set:
        key = str(directory)
        names = self._dirs.get(key)
        if names is None:
            try:
                with os.scandir(directory) as it:
                    # scandir reuses the dirent type where the OS supplies it,
                    # so this usually avoids a stat per entry.
                    names = {e.name for e in it if e.is_file()}
            except OSError:
                names = set()
            self._dirs[key] = names
        return names

    def contains(self, path: Path) -> bool:
        return path.name in self.names(path.parent)

    def add(self, path: Path) -> None:
        self.names(path.parent).add(path.name)

    def discard(self, path: Path) -> None:
        self._dirs.get(str(path.parent), set()).discard(path.name)


def find_existing_file_by_path(
    dest_dir: Path,
    product: str,
    version: str,
    filename: str,
    dir_index: "Optional[DirIndex]" = None,
) -> Optional[Path]:
    """
    Find an already-downloaded copy of `filename` under $dest/$product/$version.

    Matches the exact name, or the same name carrying a CKey collision suffix.
    The suffix test is an exact shape rather than a prefix test: tag-derived
    siblings like 'Wow-CN_Windows_x86_64.exe' share a prefix with 'Wow.exe' by
    design, and a startswith() check reports them as the same file.
    """
    index = dir_index if dir_index is not None else DirIndex()

    path_parts = filename.replace("\\", "/").split("/")
    file_dir = dest_dir / product / version
    for part in path_parts[:-1]:
        file_dir = file_dir / part
    base_name = path_parts[-1]

    names = index.names(file_dir)
    if base_name in names:
        return file_dir / base_name

    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    collision = re.compile(
        rf"^{re.escape(stem)}\.[0-9a-f]{{8}}(?:[0-9a-f]{{24}})?{re.escape(suffix)}$"
    )
    for name in names:
        if collision.match(name):
            return file_dir / name

    return None


def is_file_already_downloaded(
    dest_dir: Path,
    ckey: str,
    ckey_map: Dict[str, Dict[str, str]],
    dir_index: "Optional[DirIndex]" = None,
) -> Optional[Path]:
    """
    Check if a file with the given CKey has already been downloaded.
    Returns the Path to the existing file if found, None otherwise.
    """
    if ckey not in ckey_map:
        return None

    file_info = ckey_map[ckey]
    file_path = dest_dir / file_info["filename"]

    # Verify the file actually exists
    present = (
        dir_index.contains(file_path)
        if dir_index is not None
        else (file_path.exists() and file_path.is_file())
    )
    if present:
        return file_path

    # File doesn't exist, remove from map
    del ckey_map[ckey]
    return None


def get_ckey_for_file_path(
    dest_dir: Path, file_path: Path, ckey_map: Dict[str, Dict[str, str]]
) -> Optional[str]:
    """
    Check if a file path is already mapped to a CKey.
    Returns the CKey if found, None otherwise.
    """
    rel_path = file_path.relative_to(dest_dir)
    rel_path_str = str(rel_path)

    for existing_ckey, file_info in ckey_map.items():
        if file_info.get("filename") == rel_path_str:
            return existing_ckey

    return None


def update_ckey_map(
    dest_dir: Path,
    ckey: str,
    file_path: Path,
    product: str,
    version: str,
    ckey_map: Dict[str, Dict[str, str]],
) -> None:
    """Update the CKey mapping with a new file entry."""
    rel_path = file_path.relative_to(dest_dir)
    rel_path_str = str(rel_path)

    # Remove any old entries that point to the same file (different CKey)
    # This handles the case where a file is overwritten with new content
    keys_to_remove = []
    for existing_ckey, file_info in ckey_map.items():
        if existing_ckey != ckey and file_info.get("filename") == rel_path_str:
            keys_to_remove.append(existing_ckey)

    for key in keys_to_remove:
        del ckey_map[key]

    # Add/update entry with new CKey
    ckey_map[ckey] = {
        "filename": rel_path_str,
        "product": product,
        "version": version,
    }


async def fetch(url: str, client: httpx.AsyncClient, is_text=True):
    response = await client.get(url)
    response.raise_for_status()
    if is_text:
        return response.text
    return response.content


@click.group()
def main():
    """Blizztools in Python"""
    pass


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.option("--version-file", type=click.Path(exists=True))
@click.pass_context
def version(ctx, product, version_file):
    """Get available versions for a product."""
    asyncio.run(versions_command(product, version_file))


async def versions_command(product_name, version_file):
    product = Product[product_name]
    if version_file:
        with open(version_file, "r") as f:
            version_text = f.read()
    else:
        url = f"{BASE_URL}/{product.value}/versions"
        async with httpx.AsyncClient() as client:
            version_text = await fetch(url, client)

    version_table = parse_version_table(version_text)
    console.print(version_table)


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.pass_context
def cdn(ctx, product):
    """Get available CDNs for a product."""
    asyncio.run(cdn_command(product))


async def cdn_command(product_name):
    product = Product[product_name]
    url = f"{BASE_URL}/{product.value}/cdns"
    async with httpx.AsyncClient() as client:
        cdn_text = await fetch(url, client)

    cdn_table = parse_cdn_table(cdn_text)
    console.print(cdn_table)


class CdnPool:
    """
    Ordered CDN bases (`host/path`) with failover and a sticky preference.

    A single edge can be healthy for small objects and broken for large ones:
    level3.blizzard.com has been observed serving configs fine while dropping
    a 187 MB body after 6 MiB, when us.cdn.blizzard.com served the same object
    whole in 3.3s. Pinning one host for a whole run therefore fails everything
    downstream, so every fetch walks the pool and remembers what worked.
    """

    TRANSIENT = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.WriteError,
        httpx.PoolTimeout,
    )

    def __init__(self, candidates: List[str], attempts_per_host: int = 2):
        seen = set()
        self.candidates = [
            c for c in candidates if not (c in seen or seen.add(c))
        ]
        self.attempts_per_host = attempts_per_host
        self._preferred = 0

    def __bool__(self) -> bool:
        return bool(self.candidates)

    @property
    def primary(self) -> str:
        return self.candidates[self._preferred]

    def _ordered(self):
        n = len(self.candidates)
        for offset in range(n):
            i = (self._preferred + offset) % n
            yield i, self.candidates[i]

    async def stream_to(
        self,
        path: str,
        client: httpx.AsyncClient,
        sink,
        headers: Optional[Dict[str, str]] = None,
        accept_missing: bool = False,
    ) -> Optional[int]:
        """
        Stream `path` into `sink` (a seekable binary file) without buffering
        the body in memory. Returns bytes written, or None when every host
        reports the object missing and accept_missing is set.

        On a mid-transfer failure the sink is rewound and truncated before the
        next host is tried, so a partial body never contaminates a retry.
        """
        last_error: Optional[Exception] = None
        missing = False
        start = sink.tell()

        for index, base in self._ordered():
            url = f"https://{base}/{path}"
            for attempt in range(self.attempts_per_host):
                sink.seek(start)
                sink.truncate(start)
                written = 0
                try:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code in (403, 404):
                            missing = True
                            break
                        response.raise_for_status()
                        async for block in response.aiter_bytes(BLTE_STREAM_BLOCK):
                            sink.write(block)
                            written += len(block)
                    self._preferred = index
                    return written
                except httpx.HTTPStatusError as e:
                    last_error = e
                    break
                except self.TRANSIENT as e:
                    last_error = e
                    if attempt + 1 < self.attempts_per_host:
                        await asyncio.sleep(0.5 * (attempt + 1))

        sink.seek(start)
        sink.truncate(start)
        if missing and accept_missing:
            return None
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"No CDN host could serve {path}")

    async def get(
        self,
        path: str,
        client: httpx.AsyncClient,
        headers: Optional[Dict[str, str]] = None,
        accept_missing: bool = False,
    ) -> Optional[bytes]:
        """
        GET `path` (e.g. 'data/ab/cd/abcd...') from the first host that serves it.

        Returns None when every host reports the object missing and
        accept_missing is set; raises the last error otherwise.
        """
        last_error: Optional[Exception] = None
        missing = False

        for index, base in self._ordered():
            url = f"https://{base}/{path}"
            for attempt in range(self.attempts_per_host):
                try:
                    response = await client.get(url, headers=headers)
                    if response.status_code in (403, 404):
                        missing = True
                        break  # host-level "not here"; try the next host
                    response.raise_for_status()
                    self._preferred = index
                    return response.content
                except httpx.HTTPStatusError as e:
                    last_error = e
                    break
                except self.TRANSIENT as e:
                    last_error = e
                    if attempt + 1 < self.attempts_per_host:
                        await asyncio.sleep(0.5 * (attempt + 1))

        if missing and accept_missing:
            return None
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"No CDN host could serve {path}")


def cdn_pool_from_table(cdn_table) -> CdnPool:
    """Build a CdnPool from a parsed /cdns table: hosts first, then servers."""
    candidates: List[str] = []
    for cdn_def in cdn_table:
        for host in cdn_def.hosts:
            candidates.append(f"{host}/{cdn_def.path}")
    for cdn_def in cdn_table:
        for server in cdn_def.servers:
            host = server.split("?")[0]
            for scheme in ("https://", "http://"):
                if host.startswith(scheme):
                    host = host[len(scheme) :]
            candidates.append(f"{host.rstrip('/')}/{cdn_def.path}")
    return CdnPool(candidates)


class ArchiveResolver:
    """
    EKey -> (archive hash, offset, size) for one product's archives.

    Only a minority of CDN objects are loose `data/xx/yy/{ekey}` files; the
    rest are packed into archives, each with a companion `.index`. Building
    the full map costs one GET per archive -- 1,347 for WoW -- and retaining
    every entry costs hundreds of MB (WoW's archives hold ~4.7M objects), so:

    * `wanted` restricts what is retained to the EKeys actually being fetched;
    * the scan stops as soon as every wanted EKey has been located;
    * index blobs are cached on local disk, and since they are named by their
      own content hash they never need invalidating.

    Loading is deferred until a loose fetch actually misses.
    """

    def __init__(
        self,
        pool: "CdnPool",
        cdn_config_hash: str,
        concurrency: int = 16,
        wanted: Optional[set] = None,
        cache: "Optional[IndexCache]" = None,
    ):
        self.pool = pool
        self.cdn_config_hash = cdn_config_hash
        self._concurrency = concurrency
        self._wanted = set(wanted) if wanted else None
        self._cache = cache if cache is not None else IndexCache()
        self._index: Optional[Dict[bytes, Tuple[str, int, int]]] = None
        self._archives: Optional[List[str]] = None
        self._remaining: List[str] = []
        self._scanned = 0
        self._full_scan_done = False

    @staticmethod
    def _data_path(h: str, suffix: str = "") -> str:
        return f"data/{h[:2]}/{h[2:4]}/{h}{suffix}"

    def set_wanted(self, wanted) -> None:
        """
        Restrict what the index retains to `wanted`.

        A completed unrestricted scan already holds everything, so it is kept.
        A restricted index is discarded, since it was built for a different
        question and would answer this one wrongly.
        """
        if self._full_scan_done:
            return
        self._wanted = set(wanted) if wanted else None
        self._index = None
        self._archives = None
        self._remaining = []
        self._scanned = 0

    async def _fetch_index(self, archive: str, client) -> Optional[bytes]:
        cached = self._cache.get(archive)
        if cached is not None:
            return cached
        try:
            raw = await self.pool.get(self._data_path(archive, ".index"), client)
        except Exception:
            # One unreachable or malformed index must not sink the resolver;
            # the EKey may well live in another archive.
            return None
        if raw:
            self._cache.put(archive, raw)
        return raw

    async def _ensure_archive_list(self, client) -> None:
        if self._archives is not None:
            return
        h = self.cdn_config_hash
        raw_config = await self.pool.get(f"config/{h[:2]}/{h[2:4]}/{h}", client)
        self._archives = parse_cdn_config(raw_config).get("archives", [])
        self._remaining = list(self._archives)
        self._index = {}

    async def _scan_batch(self, client) -> int:
        """Consume the next batch of archive indices. Returns how many parsed."""
        batch = self._remaining[: self._concurrency]
        self._remaining = self._remaining[self._concurrency :]
        if not batch:
            return 0

        async def one(archive):
            raw = await self._fetch_index(archive, client)
            if raw is None:
                return None
            try:
                return archive, parse_archive_index(raw)
            except ArchiveIndexError:
                return None

        parsed = 0
        for result in await asyncio.gather(*[one(a) for a in batch]):
            if not result:
                continue
            parsed += 1
            archive, entries = result
            for key, (offset, size) in entries.items():
                if self._wanted is not None and key not in self._wanted:
                    continue
                self._index.setdefault(key, (archive, offset, size))
        self._scanned += parsed
        return parsed

    async def _locate(self, e_key: Md5Hash, client):
        """
        Find one EKey, scanning archive indices only until it turns up.

        Scanning stops at the batch that produces the key rather than at a
        count of wanted keys: the wanted set includes loose objects that are
        in no archive at all, so waiting for it to fill would always read
        every index. Progress persists, so later lookups resume where this
        one stopped.
        """
        await self._ensure_archive_list(client)

        key = e_key.data
        while key not in self._index and self._remaining:
            await self._scan_batch(client)

        location = self._index.get(key)

        if location is None and self._wanted is not None and key not in self._wanted:
            # Outside the retained set: redo unrestricted rather than claim
            # the object is absent.
            self._wanted = None
            self._archives = None
            self._index = None
            self._scanned = 0
            await self._ensure_archive_list(client)
            while key not in self._index and self._remaining:
                await self._scan_batch(client)
            location = self._index.get(key)

        if not self._remaining:
            self._full_scan_done = self._wanted is None

        return location

    async def _load(self, client: httpx.AsyncClient) -> None:
        """Scan every archive index. Used by tests and by exhaustive callers."""
        await self._ensure_archive_list(client)
        while self._remaining:
            await self._scan_batch(client)
        self._full_scan_done = self._wanted is None
        scope = (
            f"{len(self._index)}/{len(self._wanted)} wanted objects"
            if self._wanted is not None
            else f"{len(self._index)} objects"
        )
        console.print(
            f"[blue]🗄  Located {scope} after {self._scanned}/"
            f"{len(self._archives)} archive indices ({self._cache.summary})[/blue]"
        )

    def stats_line(self) -> Optional[str]:
        """One-line summary of archive work done, or None if none was needed."""
        if not self._scanned:
            return None
        total = len(self._archives) if self._archives else 0
        return (
            f"{len(self._index)} archived objects located after reading "
            f"{self._scanned}/{total} archive indices ({self._cache.summary})"
        )

    async def fetch_ekey(
        self, e_key: Md5Hash, client: httpx.AsyncClient
    ) -> Optional[bytes]:
        """Return raw BLTE bytes for an archived EKey, or None if not archived."""
        location = await self._locate(e_key, client)
        if location is None:
            return None
        archive, offset, size = location
        return await self.pool.get(
            self._data_path(archive),
            client,
            headers={"Range": f"bytes={offset}-{offset + size - 1}"},
        )

    async def stream_ekey(self, e_key: Md5Hash, client, sink) -> Optional[int]:
        """Stream an archived EKey's raw BLTE bytes into `sink`."""
        location = await self._locate(e_key, client)
        if location is None:
            return None
        archive, offset, size = location
        return await self.pool.stream_to(
            self._data_path(archive),
            client,
            sink,
            headers={"Range": f"bytes={offset}-{offset + size - 1}"},
        )


async def build_ckey_lookup(pool, encoding_ekey, resolver, wanted, client):
    """
    Resolve CKey -> EKey for just the CKeys in `wanted` (a set of raw bytes).

    Streams and decodes the encoding manifest to a temp file, then walks it
    once yielding entries, so peak memory is bounded by the number of files
    being fetched rather than the size of the manifest.
    """
    lookup = {}
    with tempfile.TemporaryDirectory() as tmp:
        decoded = Path(tmp) / "encoding"
        await download_by_ekey_to_path(pool, encoding_ekey, client, resolver, decoded)
        with open(decoded, "rb") as fh:
            for ckey, ekey in iter_ce_entries(fh):
                if ckey in wanted:
                    lookup.setdefault(ckey, ekey)
                    if len(lookup) == len(wanted):
                        break
    return lookup


async def download_by_ekey_to_path(
    pool: "CdnPool",
    e_key: Md5Hash,
    client: httpx.AsyncClient,
    resolver: "Optional[ArchiveResolver]",
    dest: Path,
) -> int:
    """
    Fetch one EKey straight to `dest`, decoding BLTE on the way.

    The encoded body lands in a sibling temp file rather than memory, and the
    decode streams chunk by chunk, so a 130 MB binary costs a chunk of RAM
    instead of three copies of the file.
    """
    e_key_str = str(e_key)
    path = f"data/{e_key_str[:2]}/{e_key_str[2:4]}/{e_key_str}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    encoded = dest.with_name(dest.name + ".blte.part")

    try:
        with open(encoded, "w+b") as staging:
            written = await pool.stream_to(
                path, client, staging, accept_missing=resolver is not None
            )
            if written is None:
                written = await resolver.stream_ekey(e_key, client, staging)
                if written is None:
                    raise FileNotFoundError(
                        f"EKey {e_key_str} is neither a loose object nor in any archive"
                    )
            staging.seek(0)
            with open(dest, "wb") as out:
                return decode_blte_stream(staging, out)
    finally:
        encoded.unlink(missing_ok=True)


async def download_by_ekey(
    pool: "CdnPool",
    e_key: Md5Hash,
    client: httpx.AsyncClient,
    resolver: "Optional[ArchiveResolver]" = None,
) -> bytes:
    e_key_str = str(e_key)
    path = f"data/{e_key_str[:2]}/{e_key_str[2:4]}/{e_key_str}"

    # Absent from every host means it is not a loose object: it lives inside
    # an archive, so fall through to the resolver rather than failing.
    blte_bytes = await pool.get(path, client, accept_missing=resolver is not None)
    if blte_bytes is None:
        blte_bytes = await resolver.fetch_ekey(e_key, client)
        if blte_bytes is None:
            raise FileNotFoundError(
                f"EKey {e_key_str} is neither a loose object nor in any archive"
            )
    return parse_blte(blte_bytes)


@main.command(name="install-manifest")
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.option("--version-file", type=click.Path(exists=True))
@click.option("--config-file", type=click.Path(exists=True))
@click.pass_context
def install_manifest_cmd(ctx, product, version_file, config_file):
    """Download and parse the install manifest."""
    asyncio.run(install_manifest_command(product, version_file, config_file))


async def install_manifest_command(
    product_name, version_file, config_file, return_data=False, index_cache=None
):
    product = Product[product_name]
    url_base = f"{BASE_URL}/{product.value}"

    async with httpx.AsyncClient(http2=USE_HTTP2) as client:
        cdn_text = await fetch(f"{url_base}/cdns", client)
        cdn_table = parse_cdn_table(cdn_text)
        if not return_data:
            print(cdn_table)

        pool = cdn_pool_from_table(cdn_table)

        if version_file:
            with open(version_file, "r") as f:
                version_text = f.read()
        else:
            version_text = await fetch(f"{url_base}/versions", client)
        version_table = parse_version_table(version_text)
        latest_version = version_table[0]

        if config_file:
            with open(config_file, "rb") as f:
                build_config_bytes = f.read()
        else:
            h = str(latest_version.build_config)
            build_config_bytes = await pool.get(
                f"config/{h[:2]}/{h[2:4]}/{h}", client
            )

        build_config = parse_build_config(build_config_bytes)

        resolver = ArchiveResolver(
            pool, str(latest_version.cdn_config), cache=index_cache
        )

        install_hash = build_config.install[1]
        table_data = await download_by_ekey(pool, install_hash, client, resolver)

        install_manifest_data = InstallManifest.parse(table_data)

        if return_data:
            # Resolve CKey -> EKey once per product. download_command would
            # otherwise refetch and reparse the whole encoding manifest for
            # every single file, which at bundle scale (105 Mac entries)
            # hammers the CDN until it drops the connection.
            # Hand back the encoding manifest's EKey rather than a parsed
            # lookup: the caller knows which CKeys it actually wants, and
            # resolving all ~2.87M of WoW's costs gigabytes of RSS.
            return (
                install_manifest_data,
                latest_version.version_name,
                pool,
                resolver,
                build_config.encoding[1],
            )

        for entry in install_manifest_data.entries:
            if entry.name:
                console.print(f"Name: {entry.name}, CKey: {entry.hash}")


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.argument("content_key", type=str)
@click.option("--output", type=click.Path(), default=".")
@click.option("--version-file", type=click.Path(exists=True))
@click.option("--config-file", type=click.Path(exists=True))
@click.pass_context
def download(ctx, product, content_key, output, version_file, config_file):
    """Download a file by its content key."""
    asyncio.run(
        download_command(product, content_key, output, version_file, config_file)
    )


async def download_command(
    product_name,
    content_key_str,
    output_dir,
    version_file,
    config_file,
    version_name=None,
    pool=None,
    return_path=False,
    resolver=None,
    ckey_lookup=None,
):
    product = Product[product_name]
    content_key = Md5Hash(content_key_str)
    url_base = f"{BASE_URL}/{product.value}"

    async with httpx.AsyncClient(http2=USE_HTTP2) as client:
        if ckey_lookup is not None:
            ekey = ckey_lookup.get(content_key.data)
            if ekey is None:
                if not return_path:
                    console.print(f"Could not find EKey for CKey {content_key}")
                return None
            return await _write_downloaded_file(
                pool,
                Md5Hash(ekey) if isinstance(ekey, bytes) else ekey,
                client,
                resolver,
                output_dir,
                product_name,
                version_name,
                content_key_str,
                return_path,
            )

        if pool is None:
            cdn_text = await fetch(f"{url_base}/cdns", client)
            pool = cdn_pool_from_table(parse_cdn_table(cdn_text))

        if version_file:
            with open(version_file, "r") as f:
                version_text = f.read()
        else:
            version_text = await fetch(f"{url_base}/versions", client)
        version_table = parse_version_table(version_text)
        latest_version = version_table[0]

        if version_name is None:
            version_name = latest_version.version_name

        if config_file:
            with open(config_file, "rb") as f:
                build_config_bytes = f.read()
        else:
            build_config_hash_str = str(latest_version.build_config)
            h = build_config_hash_str
            build_config_bytes = await pool.get(
                f"config/{h[:2]}/{h[2:4]}/{h}", client
            )
        build_config = parse_build_config(build_config_bytes)

        if resolver is None:
            resolver = ArchiveResolver(pool, str(latest_version.cdn_config))

        encoding_hash = build_config.encoding[1]
        encoding_data = await download_by_ekey(
            pool, encoding_hash, client, resolver
        )

        encoding_header, encoding_entries = parse_encoding_manifest(encoding_data)

        ekey = None
        for entry in encoding_entries:
            if entry.c_key == content_key:
                if entry.e_keys:
                    ekey = entry.e_keys[0]
                    break

        if not ekey:
            if not return_path:
                console.print(f"Could not find EKey for CKey {content_key}")
            return None

        if not return_path:
            console.print(f"Found EKey: {ekey}")
        return await _write_downloaded_file(
            pool,
            ekey,
            client,
            resolver,
            output_dir,
            product_name,
            version_name,
            content_key_str,
            return_path,
        )


async def _write_downloaded_file(
    pool,
    ekey,
    client,
    resolver,
    output_dir,
    product_name,
    version_name,
    content_key_str,
    return_path,
):
    """Fetch one EKey and stage it at $output_dir/$product/$version/$ckey."""
    output_path_obj = Path(output_dir).expanduser().resolve()
    if version_name:
        output_path_obj = output_path_obj / product_name / version_name
    output_path_obj.mkdir(parents=True, exist_ok=True)

    output_path = output_path_obj / content_key_str
    await download_by_ekey_to_path(pool, ekey, client, resolver, output_path)

    if not return_path:
        console.print(f"Successfully downloaded and wrote to {output_path}")

    return str(output_path)


@main.command()
@click.option(
    "-p",
    "--pattern",
    "patterns",
    multiple=True,
    help="Regex pattern to watch for (may be given multiple times). "
    r"Defaults to '\.pdb$' and '_loader\.dll$'.",
)
@click.option(
    "-d",
    "--dest",
    "dest_dir",
    default="./target",
    type=click.Path(),
    help="Download directory (default: ./target)",
)
@click.option(
    "-f",
    "--file",
    "product_file",
    type=click.Path(exists=True),
    help="Text file with one product name per line (overrides built-in list).",
)
@click.option(
    "--product",
    "single_product",
    help="Single product name to process (overrides built-in list and file).",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite existing files even if they have the same hash. "
    "By default, existing files are preserved.",
)
@click.option(
    "--all-products",
    "all_products",
    is_flag=True,
    default=False,
    help="Search all known product codes instead of the default subset.",
)
@click.option(
    "--index-cache/--no-index-cache",
    "use_index_cache",
    default=True,
    help="Cache archive .index blobs on local disk (default ~/.cache/blizztools, "
    "override with BLIZZTOOLS_CACHE_DIR). They are named by content hash and "
    "never go stale. On by default.",
)
@click.option(
    "--pdb-companions/--no-pdb-companions",
    "pdb_companions",
    default=True,
    help="When a matched file is a .pdb, also download the binary sharing its "
    "name (NgWebview.pdb -> NgWebview.dll), even if that binary matches no "
    "pattern. On by default.",
)
@click.pass_context
def grab(
    ctx,
    patterns,
    dest_dir,
    product_file,
    single_product,
    overwrite,
    all_products,
    pdb_companions,
    use_index_cache,
):
    """Grab PDBs / loader DLLs from Blizzard CDNs."""
    asyncio.run(
        grab_command(
            patterns,
            dest_dir,
            product_file,
            single_product,
            overwrite,
            all_products,
            pdb_companions,
            use_index_cache,
        )
    )


async def grab_command(
    patterns,
    dest_dir,
    product_file,
    single_product,
    overwrite,
    all_products=False,
    pdb_companions=True,
    use_index_cache=True,
):
    """Grab files matching patterns from Blizzard CDNs."""
    # Compile patterns
    raw_patterns = list(patterns) if patterns else [r"\.pdb$", r"_loader\.dll$"]
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in raw_patterns]

    # Resolve download directory
    dest_path = Path(dest_dir).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)

    # Load CKey mapping to avoid re-downloading existing files
    ckey_map = load_ckey_map(dest_path)

    # One cached view of the destination tree for the whole run.
    dir_index = DirIndex()

    # Archive indices are immutable and shared across products and versions,
    # so one cache serves the whole sweep.
    index_cache = IndexCache(enabled=use_index_cache)

    # Build product list
    if single_product:
        products = [single_product]
    elif product_file:
        with open(product_file, "r", encoding="utf-8") as fp:
            products = [ln.strip() for ln in fp if ln.strip()]
    elif all_products:
        products = sorted({_code_to_cli_name(c) for c in ALL_PRODUCT_CODES})
    else:
        products = DEFAULT_PRODUCTS

    # Iterate products
    for prod_name in products:
        console.print(f"▶  {prod_name}")
        product_enum = product_name_to_enum(prod_name)
        if not product_enum:
            console.print(f"[red]❌  Unknown product: {prod_name}[/red]")
            continue

        try:
            (
                install_manifest,
                version_name,
                pool,
                resolver,
                encoding_ekey,
            ) = await install_manifest_command(
                product_enum.name, None, None, return_data=True,
                index_cache=index_cache,
            )
        except Exception as e:
            console.print(
                f"[red]❌  Failed to get install manifest for {prod_name}: {e}[/red]"
            )
            continue

        selected = select_grab_entries(
            install_manifest.entries, compiled_patterns, pdb_companions
        )
        variant_names = build_variant_names(
            install_manifest.entries,
            install_manifest.tags,
            install_manifest.num_entries,
        )
        companion_count = sum(1 for _, _, is_companion in selected if is_companion)

        # Resolve EKeys for exactly the files we intend to fetch.
        wanted = {e.hash.data for _, e, _ in selected}
        ckey_lookup = {}
        if wanted:
            async with httpx.AsyncClient(http2=USE_HTTP2) as enc_client:
                ckey_lookup = await build_ckey_lookup(
                    pool, encoding_ekey, resolver, wanted, enc_client
                )
            console.print(
                f"[blue]🔑  Resolved {len(ckey_lookup)}/{len(wanted)} "
                f"CKey->EKey mappings[/blue]"
            )
            # Archived lookups only ever need these EKeys, so the resolver can
            # retain just them and stop scanning once they are all located.
            resolver.set_wanted(set(ckey_lookup.values()))
        if companion_count:
            console.print(
                f"[blue]🔗  {companion_count} companion binaries pulled in "
                f"alongside matched .pdb files[/blue]"
            )

        for entry_index, entry, is_companion in selected:
            ckey_str = str(entry.hash)
            target_name = variant_names.get(entry_index, entry.name)
            if target_name != entry.name:
                console.print(
                    f"[magenta]🏷  {entry.name} -> {target_name} "
                    f"(disambiguated by manifest tags)[/magenta]"
                )
            if is_companion:
                console.print(f"[blue]🔗  companion of a matched .pdb: {entry.name}[/blue]")

            # Check if file is already downloaded (via CKey map)
            existing_file = is_file_already_downloaded(
                dest_path, ckey_str, ckey_map, dir_index
            )
            if existing_file:
                if not overwrite:
                    # The CKey map is content-addressed, so one CKey maps to
                    # one path. A manifest may list identical content at two
                    # paths -- 47 of Wow's 52 paired bundle files are the same
                    # bytes under CN and global tags. Skipping the second one
                    # leaves the variant bundle incomplete, so materialize it
                    # from the copy already on disk instead of re-downloading.
                    wanted = dest_path / prod_name / version_name / Path(
                        target_name.replace("\\", "/")
                    )
                    if dir_index.contains(wanted):
                        console.print(
                            f"[cyan]⊘  Skipped {entry.name:<45} "
                            f"(CKey {ckey_str}) - already exists[/cyan]"
                        )
                        continue
                    if link_duplicate(existing_file, wanted):
                        dir_index.add(wanted)
                        console.print(
                            f"[green]⧉  Linked {target_name} from "
                            f"{existing_file.relative_to(dest_path)} "
                            f"(same CKey {ckey_str})[/green]"
                        )
                        continue
                    console.print(
                        f"[yellow]⚠  Could not materialize {target_name} from "
                        f"the existing copy; re-downloading[/yellow]"
                    )
                else:
                    # Overwrite flag is set, continue to download
                    console.print(
                        f"[yellow]⚠  Will overwrite {entry.name:<45} "
                        f"(CKey {ckey_str}) - file exists in map[/yellow]"
                    )

            # Check if file exists at expected path (even if map doesn't exist)
            existing_file = find_existing_file_by_path(
                dest_path, prod_name, version_name, target_name, dir_index
            )
            if existing_file:
                # Check if the existing file is already mapped to a different CKey
                existing_ckey = get_ckey_for_file_path(
                    dest_path, existing_file, ckey_map
                )

                if existing_ckey:
                    if existing_ckey == ckey_str:
                        # Same CKey, skip (shouldn't happen due to earlier check, but safe)
                        if not overwrite:
                            console.print(
                                f"[cyan]⊘  Skipped {entry.name:<45} "
                                f"(CKey {ckey_str}) - already exists[/cyan]"
                            )
                            continue
                    else:
                        # Different CKey - collision detected, proceed with download
                        # The download will handle renaming with CKey suffix
                        if not overwrite:
                            console.print(
                                f"[yellow]⚠  {target_name} exists with a different CKey and "
                                f"manifest tags do not distinguish them; falling back to a "
                                f"CKey suffix[/yellow]"
                            )
                else:
                    # File exists but not in map - could be same or different
                    # Proceed with download, which will handle collision if needed
                    if not overwrite:
                        console.print(
                            f"[yellow]⚠  File {entry.name} exists but not in map. "
                            f"Will download (will rename if collision detected)[/yellow]"
                        )
                if overwrite:
                    # Overwrite flag is set, continue to download
                    console.print(
                        f"[yellow]⚠  Will overwrite {entry.name:<45} "
                        f"(CKey {ckey_str}) at {existing_file.relative_to(dest_path)}[/yellow]"
                    )

            try:
                downloaded_path = await download_command(
                    product_enum.name,
                    ckey_str,
                    str(dest_path),
                    None,
                    None,
                    version_name=version_name,
                    pool=pool,
                    return_path=True,
                    resolver=resolver,
                    ckey_lookup=ckey_lookup,
                )

                if downloaded_path:
                    # Move to proper location with correct filename
                    downloaded_path_obj = Path(downloaded_path)
                    if downloaded_path_obj.exists():
                        # Create organized structure: $dest/$product/$version/$filename
                        target_dir = dest_path / prod_name / version_name
                        target_dir.mkdir(parents=True, exist_ok=True)

                        # Normalize path separators (handle both \ and /)
                        # Convert backslashes to forward slashes, then split and join with Path
                        normalized_name = target_name.replace("\\", "/")
                        # Build the path component by component to ensure proper directory structure
                        path_parts = normalized_name.split("/")

                        # If there are multiple parts, create directory structure
                        if len(path_parts) > 1:
                            # All parts except the last are directories
                            file_dir = target_dir
                            for part in path_parts[:-1]:
                                file_dir = file_dir / part
                            file_dir.mkdir(parents=True, exist_ok=True)
                            # Last part is the filename
                            base_filename = file_dir / path_parts[-1]
                        else:
                            # Single filename, no directory structure needed
                            base_filename = target_dir / path_parts[0]
                            base_filename.parent.mkdir(parents=True, exist_ok=True)

                        # Make filename unique if collision detected
                        proper_filename = make_unique_filename(
                            base_filename, ckey_str, dir_index
                        )
                        is_collision = proper_filename != base_filename

                        downloaded_path_obj.rename(proper_filename)
                        dir_index.discard(downloaded_path_obj)
                        dir_index.add(proper_filename)

                        # Update CKey mapping
                        update_ckey_map(
                            dest_path,
                            ckey_str,
                            proper_filename,
                            prod_name,
                            version_name,
                            ckey_map,
                        )

                        console.print(
                            f"[green]✔  Downloaded {entry.name:<45} "
                            f"(CKey {ckey_str}) for {prod_name}[/green]"
                        )
                        if is_collision:
                            rel_path = proper_filename.relative_to(dest_path)
                            console.print(
                                f"   → Renamed to {rel_path} (collision resolved with CKey)"
                            )
                        else:
                            rel_path = proper_filename.relative_to(dest_path)
                            console.print(f"   → Renamed to {rel_path}")
                    else:
                        console.print(
                            f"[red]   ⚠ Warning: Could not find downloaded file with CKey {ckey_str}[/red]"
                        )
                else:
                    console.print(
                        f"[red]   ⚠ Warning: Failed to download file {entry.name} (CKey {ckey_str})[/red]"
                    )
            except Exception as e:
                console.print(
                    f"[red]   ⚠ Warning: Error downloading {entry.name} (CKey {ckey_str}): {e}[/red]"
                )

        stats = resolver.stats_line() if resolver else None
        if stats:
            console.print(f"[blue]🗄  {stats}[/blue]")

        # Save CKey map after processing each product
        save_ckey_map(dest_path, ckey_map)


def calculate_file_md5(file_path: Path) -> str:
    """Calculate MD5 hash of a file."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def extract_product_version_from_path(
    base_dir: Path, file_path: Path
) -> Optional[Tuple[str, str]]:
    """
    Extract product and version from file path.
    Expected structure: $base_dir/$product/$version/...
    Returns (product, version) tuple or None if structure doesn't match.
    """
    try:
        rel_path = file_path.relative_to(base_dir)
        parts = rel_path.parts
        if len(parts) >= 2:
            return (parts[0], parts[1])
    except ValueError:
        # File is not under base_dir
        pass
    return None


@main.command()
@click.argument(
    "directory", type=click.Path(exists=True, file_okay=False, dir_okay=True)
)
@click.option(
    "--dest",
    "dest_dir",
    type=click.Path(file_okay=False, dir_okay=True),
    help="Directory where .ckey_map.json will be saved (should match grab --dest). "
    "Defaults to directory if not specified.",
)
@click.option(
    "--base-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Base directory for relative paths in map (defaults to dest-dir if not specified).",
)
@click.pass_context
def index(ctx, directory, dest_dir, base_dir):
    """Index files in a directory and create/update .ckey_map.json.

    The .ckey_map.json file will be saved to the --dest directory (or the directory
    being indexed if --dest is not specified). This should match the directory
    used with 'grab --dest' so that grab can find the map.
    """
    asyncio.run(index_command(directory, dest_dir, base_dir))


async def index_command(directory, dest_dir, base_dir):
    """Index files in directory and create CKey map."""
    dir_path = Path(directory).expanduser().resolve()
    # dest_path is where the map file will be saved (should match grab --dest)
    dest_path = Path(dest_dir).expanduser().resolve() if dest_dir else dir_path
    # base_path is used for extracting product/version and relative paths
    base_path = Path(base_dir).expanduser().resolve() if base_dir else dest_path

    if not dir_path.exists() or not dir_path.is_dir():
        console.print(f"[red]❌  Directory does not exist: {directory}[/red]")
        return

    dest_path.mkdir(parents=True, exist_ok=True)

    # Load existing map if it exists (from dest_path where grab would look for it)
    ckey_map = load_ckey_map(dest_path)

    console.print(f"[cyan]📁  Indexing directory: {dir_path}[/cyan]")
    console.print(f"[cyan]📁  Map will be saved to: {dest_path}[/cyan]")
    console.print(f"[cyan]📁  Base directory for paths: {base_path}[/cyan]")

    indexed_count = 0
    skipped_count = 0

    # Traverse directory recursively
    for file_path in dir_path.rglob("*"):
        if not file_path.is_file():
            continue

        # Extract product and version from path
        # First try base_path, then dir_path
        product_version = extract_product_version_from_path(base_path, file_path)
        map_base = base_path

        if not product_version:
            # Try extracting from dir_path instead if base_path doesn't match
            product_version = extract_product_version_from_path(dir_path, file_path)
            if product_version:
                map_base = dir_path
            else:
                console.print(
                    f"[yellow]⚠  Skipping {file_path.relative_to(dir_path)} - "
                    f"path structure doesn't match $base/$product/$version/...[/yellow]"
                )
                skipped_count += 1
                continue

        product, version = product_version
        rel_path = file_path.relative_to(map_base)

        # Calculate MD5 hash
        try:
            md5_hash = calculate_file_md5(file_path)
        except Exception as e:
            console.print(f"[red]⚠  Error calculating hash for {rel_path}: {e}[/red]")
            skipped_count += 1
            continue

        # Update CKey map (use map_base for relative paths)
        update_ckey_map(map_base, md5_hash, file_path, product, version, ckey_map)
        indexed_count += 1

        if indexed_count % 100 == 0:
            console.print(f"[cyan]   Indexed {indexed_count} files...[/cyan]")

    # Save the map to dest_path (where grab will look for it)
    save_ckey_map(dest_path, ckey_map)

    console.print(
        f"[green]✔  Indexed {indexed_count} files, skipped {skipped_count} files[/green]"
    )
    console.print(
        f"[green]✔  CKey map saved to {dest_path / CKEY_MAP_FILENAME}[/green]"
    )
    console.print(
        f"[yellow]💡  Note: Use the same directory with 'grab --dest {dest_path}' "
        f"so grab can find this map[/yellow]"
    )


if __name__ == "__main__":
    main()
