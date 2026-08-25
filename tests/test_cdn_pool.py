import httpx
import pytest

from blizztools.main import CdnPool, cdn_pool_from_table


class _Def:
    def __init__(self, hosts, servers, path):
        self.hosts = hosts
        self.servers = servers
        self.path = path


class _FakeClient:
    """Client whose per-URL behaviour is scripted by host."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def get(self, url, headers=None):
        host = url.split("https://", 1)[1].split("/", 1)[0]
        self.calls.append(host)
        outcome = self.behaviour[host]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            request = httpx.Request("GET", url)
            response = httpx.Response(outcome, request=request)
            if outcome in (403, 404):
                return response
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return httpx.Response(200, content=outcome, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_pool_falls_over_to_healthy_host():
    pool = CdnPool(["bad/tpr/wow", "good/tpr/wow"], attempts_per_host=1)
    client = _FakeClient(
        {
            "bad": httpx.RemoteProtocolError("peer closed connection"),
            "good": b"payload",
        }
    )
    assert await pool.get("data/aa/bb/aabb", client) == b"payload"
    assert client.calls == ["bad", "good"]


@pytest.mark.asyncio
async def test_pool_remembers_the_working_host():
    pool = CdnPool(["bad/tpr/wow", "good/tpr/wow"], attempts_per_host=1)
    client = _FakeClient(
        {"bad": httpx.RemoteProtocolError("x"), "good": b"payload"}
    )
    await pool.get("data/aa/bb/one", client)
    client.calls.clear()
    await pool.get("data/aa/bb/two", client)
    # Second fetch must start at the host that worked, not retry the bad one.
    assert client.calls == ["good"]


@pytest.mark.asyncio
async def test_pool_retries_same_host_before_moving_on():
    pool = CdnPool(["flaky/tpr/wow", "good/tpr/wow"], attempts_per_host=3)
    client = _FakeClient({"flaky": httpx.ReadError("x"), "good": b"ok"})
    await pool.get("data/aa/bb/cc", client)
    assert client.calls == ["flaky", "flaky", "flaky", "good"]


@pytest.mark.asyncio
async def test_pool_missing_everywhere_returns_none_when_allowed():
    pool = CdnPool(["a/p", "b/p"], attempts_per_host=1)
    client = _FakeClient({"a": 404, "b": 403})
    assert await pool.get("data/aa/bb/cc", client, accept_missing=True) is None


@pytest.mark.asyncio
async def test_pool_missing_everywhere_raises_when_not_allowed():
    pool = CdnPool(["a/p"], attempts_per_host=1)
    client = _FakeClient({"a": 404})
    with pytest.raises(Exception):
        await pool.get("data/aa/bb/cc", client)


@pytest.mark.asyncio
async def test_pool_404_does_not_consume_retries():
    pool = CdnPool(["a/p", "b/p"], attempts_per_host=3)
    client = _FakeClient({"a": 404, "b": b"ok"})
    assert await pool.get("data/aa/bb/cc", client) == b"ok"
    # A definitive 404 means "not on this host"; retrying it is wasted work.
    assert client.calls == ["a", "b"]


def test_pool_dedupes_candidates():
    pool = CdnPool(["a/p", "a/p", "b/p"])
    assert pool.candidates == ["a/p", "b/p"]


def test_cdn_pool_from_table_orders_hosts_before_servers():
    table = [
        _Def(["h1", "h2"], ["http://s1/?x=1"], "tpr/wow"),
        _Def(["h3"], ["https://s2/"], "tpr/wow"),
    ]
    pool = cdn_pool_from_table(table)
    assert pool.candidates == [
        "h1/tpr/wow",
        "h2/tpr/wow",
        "h3/tpr/wow",
        "s1/tpr/wow",
        "s2/tpr/wow",
    ]


class _StreamResponse:
    def __init__(self, status, blocks):
        self.status_code = status
        self._blocks = blocks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://x/"),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self, size=None):
        for block in self._blocks:
            if isinstance(block, Exception):
                raise block
            yield block


class _StreamClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def stream(self, method, url, headers=None):
        host = url.split("https://", 1)[1].split("/", 1)[0]
        self.calls.append(host)
        status, blocks = self.behaviour[host]
        return _StreamResponse(status, blocks)


@pytest.mark.asyncio
async def test_stream_to_writes_body(tmp_path):
    pool = CdnPool(["good/p"], attempts_per_host=1)
    client = _StreamClient({"good": (200, [b"abc", b"def"])})
    with open(tmp_path / "o", "w+b") as fh:
        n = await pool.stream_to("data/aa/bb/cc", client, fh)
    assert n == 6
    assert (tmp_path / "o").read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_stream_to_truncates_partial_body_before_failover(tmp_path):
    # The bad host delivers 3 bytes then dies; those must not survive.
    pool = CdnPool(["bad/p", "good/p"], attempts_per_host=1)
    client = _StreamClient(
        {
            "bad": (200, [b"XXX", httpx.RemoteProtocolError("peer closed")]),
            "good": (200, [b"clean"]),
        }
    )
    with open(tmp_path / "o", "w+b") as fh:
        n = await pool.stream_to("data/aa/bb/cc", client, fh)
    assert n == 5
    assert (tmp_path / "o").read_bytes() == b"clean"


@pytest.mark.asyncio
async def test_stream_to_preserves_existing_sink_content(tmp_path):
    # Streaming appends from the current offset and only rolls back its own.
    pool = CdnPool(["bad/p", "good/p"], attempts_per_host=1)
    client = _StreamClient(
        {
            "bad": (200, [b"YY", httpx.ReadError("x")]),
            "good": (200, [b"tail"]),
        }
    )
    with open(tmp_path / "o", "w+b") as fh:
        fh.write(b"KEEP")
        await pool.stream_to("data/aa/bb/cc", client, fh)
    assert (tmp_path / "o").read_bytes() == b"KEEPtail"


@pytest.mark.asyncio
async def test_stream_to_missing_returns_none(tmp_path):
    pool = CdnPool(["a/p"], attempts_per_host=1)
    client = _StreamClient({"a": (404, [])})
    with open(tmp_path / "o", "w+b") as fh:
        assert await pool.stream_to("d", client, fh, accept_missing=True) is None
    assert (tmp_path / "o").read_bytes() == b""
