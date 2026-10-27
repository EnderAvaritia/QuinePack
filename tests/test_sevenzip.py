"""Tests for the in-memory 7z quine writer."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from quinepack.errors import SevenZipMissingError
from quinepack.formats.sevenzip import (
    BuildResult,
    build_archive,
    verify_built,
)
from quinepack.model import Payload, PayloadEntry
from quinepack.verify import find_seven_zip

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_seven_zip() -> Path | None:
    try:
        return find_seven_zip()
    except SevenZipMissingError:
        pass
    candidates = [REPO_ROOT / "_tmp" / "7zextra" / "x64" / "7za.exe"]
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "7-Zip" / "7z.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "7-Zip" / "7z.exe")
    return next((path for path in candidates if path.is_file()), None)


SEVEN_ZIP = _find_seven_zip()
MTIME_A = 1_600_000_000_000_000_000
MTIME_B = 1_600_000_123_456_789_000


def _file(path: str, data: bytes, mtime_ns: int = MTIME_A) -> PayloadEntry:
    return PayloadEntry(path, False, data, mtime_ns)


def _dir(path: str, mtime_ns: int = MTIME_A) -> PayloadEntry:
    return PayloadEntry(path, True, None, mtime_ns)


def _payload(
    entries: tuple[PayloadEntry, ...], *, single_file: bool
) -> Payload:
    return Payload(
        source=Path("input"),
        entries=entries,
        total_bytes=sum(len(entry.data or b"") for entry in entries),
        is_single_file=single_file,
    )


def test_small_text_file_compressed_roundtrip() -> None:
    entry = _file("notes.txt", b"hello quinepack\n" * 64)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="notes.txt.7z",
        seed_name=".quinepack/notes.txt.7z.seed",
        compressed=True,
    )

    verify_built(result, payload)
    assert result.compressed is True
    assert result.data.startswith(b"7z\xbc\xaf'\x1c")
    assert result.entries == (
        (".quinepack", True),
        (".quinepack/notes.txt.7z.seed", False),
        ("notes.txt", False),
        ("notes.txt.7z", False),
    )
    assert result.files == (("notes.txt", b"hello quinepack\n" * 64),)


def test_small_binary_file_store_roundtrip() -> None:
    data = bytes(range(256)) * 3 + b"\x00\xff" * 17
    entry = _file("blob.bin", data)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="blob.bin.store.7z",
        seed_name=".quinepack/blob.bin.store.7z.seed",
        compressed=False,
    )

    verify_built(result, payload)
    assert result.compressed is False
    assert result.files == (("blob.bin", data),)
    # Store-only archives carry the payload almost verbatim.
    assert len(result.data) >= len(data)


def test_directory_nested_and_empty_dir_compressed() -> None:
    entries = (
        _dir("assets"),
        _dir("assets/empty"),
        _file("assets/nested.bin", b"\xde\xad\xbe\xef" * 200, MTIME_B),
        _file("index.txt", b"index\n" * 32, MTIME_B),
    )
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="site.7z",
        seed_name=".quinepack/site.7z.seed",
        compressed=True,
    )

    verify_built(result, payload)
    assert result.entries == (
        (".quinepack", True),
        (".quinepack/site.7z.seed", False),
        ("assets", True),
        ("assets/empty", True),
        ("assets/nested.bin", False),
        ("index.txt", False),
        ("site.7z", False),
    )
    assert result.files == (
        ("assets/nested.bin", b"\xde\xad\xbe\xef" * 200),
        ("index.txt", b"index\n" * 32),
    )


def _lcg_bytes(length: int) -> bytes:
    """Deterministic, near-incompressible bytes for multi-chunk coverage."""
    out = bytearray(length)
    value = 1
    for index in range(length):
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        out[index] = (value >> 16) & 0xFF
    return bytes(out)


def test_multi_chunk_content_roundtrip() -> None:
    data = _lcg_bytes(131_072)
    entry = _file("large.bin", data)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="large.bin.7z",
        seed_name=".quinepack/large.bin.7z.seed",
        compressed=True,
    )

    verify_built(result, payload)
    assert result.files == (("large.bin", data),)


def test_seed_parent_directory_is_not_duplicated() -> None:
    entries = (
        _dir(".quinepack"),
        _file("keep.txt", b"keep\n" * 20),
    )
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="keep.7z",
        seed_name=".quinepack/keep.7z.seed",
        compressed=True,
    )

    verify_built(result, payload)
    assert result.entries == (
        (".quinepack/keep.7z.seed", False),
        (".quinepack", True),
        ("keep.txt", False),
        ("keep.7z", False),
    )
    assert [name for name, _ in result.entries].count(".quinepack") == 1


def test_build_is_deterministic() -> None:
    entries = (
        _dir("a"),
        _file("a/one.txt", b"one" * 50, MTIME_A),
        _file("two.bin", b"\x01\x02\x03\x04" * 80, MTIME_B),
    )
    payload = _payload(entries, single_file=False)

    first = build_archive(
        payload,
        quine_name="same.7z",
        seed_name=".quinepack/same.7z.seed",
        compressed=True,
    )
    second = build_archive(
        payload,
        quine_name="same.7z",
        seed_name=".quinepack/same.7z.seed",
        compressed=True,
    )
    assert first.data == second.data
    assert first.seed == second.seed

    store_first = build_archive(
        payload,
        quine_name="same.store.7z",
        seed_name=".quinepack/same.store.7z.seed",
        compressed=False,
    )
    store_second = build_archive(
        payload,
        quine_name="same.store.7z",
        seed_name=".quinepack/same.store.7z.seed",
        compressed=False,
    )
    assert store_first.data == store_second.data


@pytest.mark.skipif(SEVEN_ZIP is None, reason="official 7-Zip is not available")
def test_external_7za_accepts_archives() -> None:
    payload = _payload(
        (
            _dir("docs"),
            _file("docs/readme.txt", b"quinepack external test\n" * 40, MTIME_B),
            _file("empty.txt", b"", MTIME_A),
        ),
        single_file=False,
    )

    scratch_root = REPO_ROOT / "_tmp"
    scratch_root.mkdir(exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sevenzip-test-", dir=scratch_root))
    try:
        for compressed in (True, False):
            result: BuildResult = build_archive(
                payload,
                quine_name="site.7z" if compressed else "site.store.7z",
                seed_name=".quinepack/site.7z.seed",
                compressed=compressed,
            )
            verify_built(result, payload)
            archive = workdir / f"archive-{int(compressed)}.7z"
            archive.write_bytes(result.data)
            completed = subprocess.run(
                [str(SEVEN_ZIP), "t", str(archive)],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert completed.returncode == 0, (
                f"7za t failed for compressed={compressed}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)