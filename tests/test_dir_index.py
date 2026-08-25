from pathlib import Path

from blizztools.main import DirIndex, find_existing_file_by_path, make_unique_filename


def test_lists_once_and_answers_from_memory(tmp_path, monkeypatch):
    d = tmp_path / "v"
    d.mkdir()
    (d / "a.exe").write_bytes(b"x")
    (d / "b.exe").write_bytes(b"x")

    idx = DirIndex()
    import blizztools.main as m

    calls = []
    real = m.os.scandir

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(m.os, "scandir", counting)
    for _ in range(10):
        idx.names(d)
    assert len(calls) == 1


def test_ignores_subdirectories(tmp_path):
    d = tmp_path / "v"
    (d / "sub").mkdir(parents=True)
    (d / "f.bin").write_bytes(b"x")
    assert DirIndex().names(d) == {"f.bin"}


def test_missing_directory_is_empty(tmp_path):
    assert DirIndex().names(tmp_path / "nope") == set()


def test_add_and_discard_keep_cache_truthful(tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    idx = DirIndex()
    assert idx.names(d) == set()
    idx.add(d / "new.exe")
    assert idx.contains(d / "new.exe")
    idx.discard(d / "new.exe")
    assert not idx.contains(d / "new.exe")


def test_find_existing_uses_shared_index(tmp_path):
    d = tmp_path / "wow" / "1.0"
    d.mkdir(parents=True)
    (d / "Wow.a1b2c3d4.exe").write_bytes(b"x")
    idx = DirIndex()
    found = find_existing_file_by_path(tmp_path, "wow", "1.0", "Wow.exe", idx)
    assert found is not None and found.name == "Wow.a1b2c3d4.exe"


def test_find_existing_still_ignores_tag_siblings(tmp_path):
    d = tmp_path / "wow" / "1.0"
    d.mkdir(parents=True)
    (d / "Wow-CN_Windows_x86_64.exe").write_bytes(b"x")
    idx = DirIndex()
    assert find_existing_file_by_path(tmp_path, "wow", "1.0", "Wow.exe", idx) is None


def test_find_existing_handles_nested_and_backslash_paths(tmp_path):
    d = tmp_path / "wow" / "1.0" / "App.app" / "Contents"
    d.mkdir(parents=True)
    (d / "PkgInfo").write_bytes(b"x")
    idx = DirIndex()
    found = find_existing_file_by_path(
        tmp_path, "wow", "1.0", r"App.app\Contents\PkgInfo", idx
    )
    assert found is not None and found.name == "PkgInfo"


def test_find_existing_without_index_still_works(tmp_path):
    d = tmp_path / "wow" / "1.0"
    d.mkdir(parents=True)
    (d / "Wow.exe").write_bytes(b"x")
    assert find_existing_file_by_path(tmp_path, "wow", "1.0", "Wow.exe") is not None


def test_make_unique_filename_uses_index_not_disk(tmp_path):
    # The index knows about a file that is not on disk yet; the name must
    # still be treated as taken.
    idx = DirIndex()
    idx.names(tmp_path)
    target = tmp_path / "Wow.exe"
    idx.add(target)
    got = make_unique_filename(target, "a" * 32, idx)
    assert got.name == f"Wow.{'a' * 8}.exe"


def test_make_unique_filename_without_index_matches_old_behaviour(tmp_path):
    target = tmp_path / "Wow.exe"
    assert make_unique_filename(target, "a" * 32) == target
    target.write_bytes(b"x")
    assert make_unique_filename(target, "a" * 32).name == f"Wow.{'a' * 8}.exe"
