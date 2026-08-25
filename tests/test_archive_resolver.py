import struct

import pytest

from blizztools.cache import IndexCache
from blizztools.main import ArchiveResolver, Md5Hash
from tests.test_archives import build_index


class _FakePool:
    def __init__(self, archives, contents):
        self.archives = archives
        self.contents = contents
        self.fetched = []

    async def get(self, path, client, headers=None, accept_missing=False):
        if path.startswith("config/"):
            return ("archives = " + " ".join(self.archives)).encode()
        name = path.rsplit("/", 1)[1]
        if name.endswith(".index"):
            archive = name[: -len(".index")]
            self.fetched.append(archive)
            return self.contents[archive]
        return b"body"


def _key(n):
    return bytes([n]) * 16


def _archives(count, per=3):
    """count archives, each holding `per` distinct keys."""
    contents, names, owner = {}, [], {}
    k = 0
    for a in range(count):
        name = f"{a:032x}"
        entries = []
        for _ in range(per):
            entries.append((_key(k), k * 1000, 500))
            owner[_key(k)] = name
            k += 1
        contents[name] = build_index(entries)
        names.append(name)
    return names, contents, owner


@pytest.mark.asyncio
async def test_resolver_finds_key(tmp_path):
    names, contents, owner = _archives(4)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, cache=IndexCache(tmp_path))
    await r._load(None)
    assert r._index[_key(0)][0] == owner[_key(0)]


@pytest.mark.asyncio
async def test_resolver_retains_only_wanted(tmp_path):
    names, contents, _ = _archives(5)
    pool = _FakePool(names, contents)
    wanted = {_key(1), _key(7)}
    r = ArchiveResolver(pool, "c" * 32, wanted=wanted, cache=IndexCache(tmp_path))
    await r._load(None)
    assert set(r._index) == wanted


@pytest.mark.asyncio
async def test_locate_stops_scanning_once_the_key_is_found(tmp_path):
    names, contents, _ = _archives(40)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(
        pool,
        "c" * 32,
        wanted={_key(0), _key(1)},
        concurrency=2,
        cache=IndexCache(tmp_path),
    )
    # _key(0) lives in the very first archive.
    assert await r._locate(Md5Hash(_key(0)), None) is not None
    assert len(pool.fetched) <= 2, f"scanned {len(pool.fetched)} archives"


@pytest.mark.asyncio
async def test_locate_resumes_where_the_last_scan_stopped(tmp_path):
    names, contents, owner = _archives(20)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, concurrency=2, cache=IndexCache(tmp_path))

    await r._locate(Md5Hash(_key(0)), None)
    after_first = len(pool.fetched)

    # A key in a much later archive continues the scan rather than restarting.
    located = await r._locate(Md5Hash(_key(50)), None)
    assert located is not None and located[0] == owner[_key(50)]
    assert len(set(pool.fetched)) == len(pool.fetched), "an index was fetched twice"
    assert len(pool.fetched) > after_first


@pytest.mark.asyncio
async def test_locate_does_not_read_every_index_for_a_loose_wanted_set(tmp_path):
    """
    The wanted set includes loose objects that live in no archive. Waiting for
    it to fill would read every index, which is what this guards against.
    """
    names, contents, _ = _archives(30)
    pool = _FakePool(names, contents)
    never_archived = bytes([0xFE]) * 16
    r = ArchiveResolver(
        pool,
        "c" * 32,
        wanted={_key(0), never_archived},
        concurrency=2,
        cache=IndexCache(tmp_path),
    )
    assert await r._locate(Md5Hash(_key(0)), None) is not None
    assert len(pool.fetched) <= 2, f"scanned {len(pool.fetched)} archives"


@pytest.mark.asyncio
async def test_locate_returns_none_for_a_truly_absent_key(tmp_path):
    names, contents, _ = _archives(5)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, cache=IndexCache(tmp_path))
    assert await r._locate(Md5Hash(bytes([0xEE]) * 16), None) is None
    assert len(pool.fetched) == 5, "must exhaust archives before reporting absent"


@pytest.mark.asyncio
async def test_second_resolver_serves_indices_from_cache(tmp_path):
    names, contents, _ = _archives(6)
    cache = IndexCache(tmp_path)

    first = _FakePool(names, contents)
    await ArchiveResolver(first, "c" * 32, cache=cache)._load(None)
    assert len(first.fetched) == 6

    second = _FakePool(names, contents)
    await ArchiveResolver(second, "c" * 32, cache=cache)._load(None)
    assert second.fetched == [], "indices should come from disk, not the network"


@pytest.mark.asyncio
async def test_restricted_index_rebuilds_when_asked_for_another_key(tmp_path):
    names, contents, owner = _archives(6)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, wanted={_key(0)}, cache=IndexCache(tmp_path))
    await r._load(None)
    assert set(r._index) == {_key(0)}
    # A key outside the wanted set must trigger a full rebuild, not "absent".
    located = await r._locate(Md5Hash(_key(11)), None)
    assert located is not None and located[0] == owner[_key(11)]
    assert r._full_scan_done is True


@pytest.mark.asyncio
async def test_full_scan_is_not_discarded_by_set_wanted(tmp_path):
    names, contents, _ = _archives(4)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, cache=IndexCache(tmp_path))
    await r._load(None)
    before = dict(r._index)
    r.set_wanted({_key(0)})
    assert r._index == before


@pytest.mark.asyncio
async def test_set_wanted_discards_a_restricted_index(tmp_path):
    names, contents, _ = _archives(4)
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, wanted={_key(0)}, cache=IndexCache(tmp_path))
    await r._load(None)
    r.set_wanted({_key(5)})
    assert r._index is None, "a restricted index answers a different question"


@pytest.mark.asyncio
async def test_malformed_index_does_not_sink_the_scan(tmp_path):
    names, contents, owner = _archives(3)
    contents[names[1]] = b"garbage"
    pool = _FakePool(names, contents)
    r = ArchiveResolver(pool, "c" * 32, cache=IndexCache(tmp_path))
    await r._load(None)
    # Keys from the healthy archives are still located.
    assert r._index[_key(0)][0] == owner[_key(0)]
    assert r._index[_key(6)][0] == owner[_key(6)]
