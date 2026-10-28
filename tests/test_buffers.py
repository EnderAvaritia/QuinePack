"""Tests for temp-file/mmap backed buffers."""

from __future__ import annotations

from pathlib import Path

import pytest

from quinepack import scan
from quinepack.buffers import ContentStore, SpoolWriter
from quinepack.formats import sevenzip, tarxz
from quinepack.model import Payload, PayloadEntry
from quinepack.scan import scan_payload


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_content_store_migrates_and_preserves_bytes() -> None:
    store = ContentStore(threshold=4)
    store.append(b"ab")
    store.append(b"cdef")
    store.append(memoryview(b"gh"))
    try:
        assert store.size == 8
        assert bytes(store.view()) == b"abcdefgh"
    finally:
        store.close()


def test_spool_writer_finish_returns_bytes_when_small() -> None:
    writer = SpoolWriter(threshold=16)
    writer.write(b"small")
    data, handle = writer.finish()
    assert data == b"small"
    assert handle is None


def test_scan_spools_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "SPOOL_THRESHOLD", 1)
    source = _write(tmp_path / "data.bin", bytes(range(256)) * 4)

    payload = scan_payload(source)
    try:
        assert isinstance(payload.entries[0].data, memoryview)
        assert bytes(payload.entries[0].data) == source.read_bytes()
        assert bytes(payload.content_view) == source.read_bytes()
    finally:
        payload.close()


def test_scan_spooled_directory_content_matches_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "SPOOL_THRESHOLD", 0)
    root = tmp_path / "site"
    _write(root / "b.txt", b"BB")
    _write(root / "a.txt", b"A")
    _write(root / "sub" / "c.txt", b"CCC")

    payload = scan_payload(root)
    try:
        by_path = {entry.path: bytes(entry.data) for entry in payload.files}
        assert by_path == {"a.txt": b"A", "b.txt": b"BB", "sub/c.txt": b"CCC"}
        assert bytes(payload.content_view) == b"ABBCCC"
    finally:
        payload.close()


def _payload(data: bytes) -> Payload:
    entry = PayloadEntry("data.bin", False, data, 1_600_000_000_000_000_000)
    return Payload(Path("input"), (entry,), len(data), True)


def test_forced_spooled_7z_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sevenzip, "SPOOL_THRESHOLD", 0)
    payload = _payload(bytes(range(256)) * 512)

    result = sevenzip.build_archive(
        payload,
        quine_name="data.bin.7z",
        seed_name=".quinepack/data.bin.7z.seed",
        compressed=True,
    )
    sevenzip.verify_built(result, payload)


def test_forced_spooled_tarxz_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tarxz, "SPOOL_THRESHOLD", 0)
    payload = _payload(bytes(range(256)) * 512)

    result = tarxz.build_archive(
        payload,
        quine_name="data.bin.tar.xz",
        seed_name=".quinepack/data.bin.tar.xz.seed",
    )
    tarxz.verify_built(result, payload)


def test_spooled_build_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan, "SPOOL_THRESHOLD", 0)
    monkeypatch.setattr(sevenzip, "SPOOL_THRESHOLD", 0)
    source = _write(tmp_path / "data.bin", bytes(range(256)) * 512)
    payload = scan_payload(source)
    try:
        sevenzip.build_archive(
            payload,
            quine_name="data.bin.7z",
            seed_name=".quinepack/data.bin.7z.seed",
            compressed=True,
            release_payload=True,
        )
    finally:
        payload.close()

    assert list(tmp_path.glob(".quinepack-spool-*")) == []


def test_compact_7z_matches_seeded_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan, "SPOOL_THRESHOLD", 0)
    monkeypatch.setattr(sevenzip, "SPOOL_THRESHOLD", 0)
    source = _write(tmp_path / "data.bin", bytes(range(256)) * 512)

    payload = scan_payload(source)
    try:
        normal = sevenzip.build_archive(
            payload,
            quine_name="data.bin.7z",
            seed_name=".quinepack/data.bin.7z.seed",
            compressed=True,
        )
    finally:
        payload.close()

    payload = scan_payload(source)
    try:
        compact = sevenzip.build_archive(
            payload,
            quine_name="data.bin.7z",
            seed_name=".quinepack/data.bin.7z.seed",
            compressed=True,
            release_payload=True,
        )
    finally:
        payload.close()

    assert compact.data == normal.data
    assert compact.seed == b""
    assert list(tmp_path.glob(".quinepack-spool-*")) == []


def test_compact_7z_multifile_matches_seeded_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan, "SPOOL_THRESHOLD", 0)
    monkeypatch.setattr(sevenzip, "SPOOL_THRESHOLD", 0)
    source = tmp_path / "payload"
    source.mkdir()
    _write(source / "a.bin", bytes(range(256)) * 1024)
    _write(source / "b.bin", bytes(reversed(range(256))) * 1024)

    payload = scan_payload(source)
    try:
        normal = sevenzip.build_archive(
            payload,
            quine_name="payload.7z",
            seed_name=".quinepack/payload.7z.seed",
            compressed=True,
        )
    finally:
        payload.close()

    payload = scan_payload(source)
    try:
        compact = sevenzip.build_archive(
            payload,
            quine_name="payload.7z",
            seed_name=".quinepack/payload.7z.seed",
            compressed=True,
            release_payload=True,
        )
    finally:
        payload.close()

    assert compact.data == normal.data
    assert compact.seed == b""
    assert list(tmp_path.glob(".quinepack-spool-*")) == []

