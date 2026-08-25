import io
import zlib

import pytest

from blizztools.blte import decode_blte_stream, parse_blte


def build_blte(chunks):
    """Build a BLTE container from [(mode, payload)] where mode is b'N'/b'Z'."""
    encoded = []
    for mode, payload in chunks:
        body = payload if mode == b"N" else zlib.compress(payload)
        encoded.append((mode, body, len(payload)))

    header_size = 12 + len(encoded) * 24
    out = bytearray(b"BLTE")
    out += header_size.to_bytes(4, "big")
    out += bytes([0x0F, 0x00]) + len(encoded).to_bytes(2, "big")
    for mode, body, dsize in encoded:
        out += (len(body) + 1).to_bytes(4, "big")
        out += dsize.to_bytes(4, "big")
        out += b"\x00" * 16
    for mode, body, _ in encoded:
        out += mode + body
    return bytes(out)


def roundtrip(chunks):
    raw = build_blte(chunks)
    dst = io.BytesIO()
    written = decode_blte_stream(io.BytesIO(raw), dst)
    return raw, dst.getvalue(), written


def test_stream_matches_buffered_plain():
    raw, streamed, written = roundtrip([(b"N", b"hello world")])
    assert streamed == b"hello world"
    assert written == len(streamed)
    assert streamed == parse_blte(raw)


def test_stream_matches_buffered_zlib():
    payload = b"compress me " * 5000
    raw, streamed, _ = roundtrip([(b"Z", payload)])
    assert streamed == payload
    assert streamed == parse_blte(raw)


def test_stream_matches_buffered_multi_chunk_mixed():
    chunks = [
        (b"N", b"A" * 100),
        (b"Z", b"B" * 300000),
        (b"N", b"C" * 50),
        (b"Z", bytes(range(256)) * 1000),
    ]
    raw, streamed, _ = roundtrip(chunks)
    expected = b"".join(c[1] for c in chunks)
    assert streamed == expected
    assert streamed == parse_blte(raw)


def test_stream_handles_chunk_larger_than_block():
    # Force multiple read() rounds inside one chunk.
    payload = bytes(range(256)) * 20000  # ~5 MB, > STREAM_BLOCK
    raw, streamed, _ = roundtrip([(b"N", payload)])
    assert streamed == payload


def test_stream_rejects_bad_magic():
    with pytest.raises(ValueError):
        decode_blte_stream(io.BytesIO(b"XXXX" + b"\x00" * 20), io.BytesIO())


def test_stream_rejects_unknown_encoding_mode():
    raw = bytearray(build_blte([(b"N", b"x")]))
    raw[12 + 24] = ord("F")  # Recursive: not supported
    with pytest.raises(NotImplementedError):
        decode_blte_stream(io.BytesIO(bytes(raw)), io.BytesIO())


def test_stream_detects_truncated_chunk():
    raw = build_blte([(b"N", b"payload" * 100)])
    with pytest.raises(ValueError):
        decode_blte_stream(io.BytesIO(raw[:-200]), io.BytesIO())


def test_stream_detects_truncated_chunk_table():
    raw = build_blte([(b"N", b"a"), (b"N", b"b")])
    with pytest.raises(ValueError):
        decode_blte_stream(io.BytesIO(raw[:20]), io.BytesIO())


def test_stream_headerless_plain():
    raw = b"BLTE" + (0).to_bytes(4, "big") + b"N" + b"raw bytes here"
    dst = io.BytesIO()
    decode_blte_stream(io.BytesIO(raw), dst)
    assert dst.getvalue() == b"raw bytes here"


def test_stream_headerless_zlib():
    payload = b"z" * 10000
    raw = b"BLTE" + (0).to_bytes(4, "big") + b"Z" + zlib.compress(payload)
    dst = io.BytesIO()
    decode_blte_stream(io.BytesIO(raw), dst)
    assert dst.getvalue() == payload


def test_stream_peak_memory_is_bounded(tmp_path):
    # 40 MB decoded across chunks; the decoder must never hold it all.
    import tracemalloc

    chunks = [(b"Z", bytes(1 << 20) ) for _ in range(40)]
    raw = build_blte(chunks)
    src = tmp_path / "in.blte"
    src.write_bytes(raw)
    out = tmp_path / "out.bin"

    tracemalloc.start()
    with open(src, "rb") as fh, open(out, "wb") as dst:
        decode_blte_stream(fh, dst)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert out.stat().st_size == 40 << 20
    # Comfortably under the 40 MB output; buffering would exceed it.
    assert peak < 16 << 20, f"peak {peak} bytes"
