import os

import pytest

from blizztools.cache import IndexCache, default_cache_dir


def test_roundtrip(tmp_path):
    c = IndexCache(tmp_path)
    assert c.get("a" * 32) is None
    c.put("a" * 32, b"payload")
    assert c.get("a" * 32) == b"payload"
    assert c.hits == 1 and c.misses == 1


def test_shards_by_prefix(tmp_path):
    c = IndexCache(tmp_path)
    key = "abcdef" + "0" * 26
    c.put(key, b"x")
    assert (tmp_path / "index" / "ab" / f"{key}.index").exists()


def test_disabled_cache_is_inert(tmp_path):
    c = IndexCache(tmp_path, enabled=False)
    c.put("a" * 32, b"x")
    assert c.get("a" * 32) is None
    assert not (tmp_path / "index").exists()


def test_put_leaves_no_temp_files(tmp_path):
    c = IndexCache(tmp_path)
    c.put("b" * 32, b"data")
    leftovers = [p for p in (tmp_path / "index").rglob("*.tmp")]
    assert leftovers == []


def test_unreadable_root_degrades_to_miss(tmp_path):
    # A cache that cannot be written must not raise; it just never hits.
    target = tmp_path / "file"
    target.write_text("not a directory")
    c = IndexCache(target)
    c.put("c" * 32, b"x")
    assert c.get("c" * 32) is None


def test_clear_removes_entries(tmp_path):
    c = IndexCache(tmp_path)
    c.put("d" * 32, b"x")
    c.clear()
    assert c.get("d" * 32) is None


def test_default_cache_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BLIZZTOOLS_CACHE_DIR", str(tmp_path / "custom"))
    assert default_cache_dir() == tmp_path / "custom"
    monkeypatch.delenv("BLIZZTOOLS_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "blizztools"


def test_summary_reports_hit_rate(tmp_path):
    c = IndexCache(tmp_path)
    assert "unused" in c.summary
    c.put("e" * 32, b"x")
    c.get("e" * 32)
    c.get("f" * 32)
    assert c.summary == "index cache 1/2 hits"
