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
    _StoreBuilder,
    build_archive,
    verify_built,
)
from quinepack.lzma2 import CHUNK, LZ2_MAX_UNPACKED
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


class _LenOnlyContent:
    """Content stub: ``layout`` only needs ``len(content)``, not the bytes."""

    def __init__(self, length: int) -> None:
        self._length = length

    def __len__(self) -> int:
        return self._length


def _converged_store_layout(content_len: int) -> tuple[dict[str, object], int]:
    builder = _StoreBuilder((), (), _LenOnlyContent(content_len))
    d_seed = 64
    for _ in range(10):
        d = d_seed + content_len
        layout = builder.layout(d_seed, 2 * d + (1 << 21))
        need_ds = int(layout["need_ds"])
        if d_seed >= need_ds:
            return layout, d_seed
        d_seed = need_ds
    raise AssertionError("store layout did not converge")


def _assert_gadget_slip_is_minus_three(layout: dict[str, object]) -> None:
    chunks = list(layout["chunks"])
    jumps = [chunk for chunk in chunks if chunk.get("jump")]
    vacs = [chunk for chunk in chunks if chunk.get("vac")]
    assert len(jumps) == 1
    assert len(vacs) == 1
    jump, vac = jumps[0], vacs[0]
    jump_y = int(layout["jump_y"])
    d = int(layout["d"])
    assert int(vac["foff"]) == (int(jump["ooff"]) + jump_y - d) - 3


def test_store_layout_keeps_gear_branch_at_32mib() -> None:
    layout, d_seed = _converged_store_layout(32 * 1024 * 1024)
    jump_y = int(layout["jump_y"])

    # The small branch must keep the original 64-byte seed and small jump.
    assert d_seed == 64
    assert int(layout["need_ds"]) == 0
    assert jump_y + 6 <= 64
    assert jump_y <= CHUNK

    gear_stores = [
        chunk
        for chunk in layout["chunks"]
        if chunk["kind"] == "store"
        and not chunk.get("c1")
        and not chunk.get("vac")
    ]
    assert gear_stores, "expected at least one gear store chunk"
    _assert_gadget_slip_is_minus_three(layout)


def test_store_layout_plants_copy_when_slip_exceeds_chunk() -> None:
    layout, d_seed = _converged_store_layout(48 * 1024 * 1024)
    jump_y = int(layout["jump_y"])
    need_ds = int(layout["need_ds"])

    # The oversized gear pair is impossible; the planted branch must take over.
    assert need_ds == jump_y + 6
    assert need_ds > 64
    assert jump_y > CHUNK
    assert jump_y < LZ2_MAX_UNPACKED

    chunks = list(layout["chunks"])
    jumps = [chunk for chunk in chunks if chunk.get("jump")]
    vacs = [chunk for chunk in chunks if chunk.get("vac")]
    assert len(jumps) == 1
    jump, vac = jumps[0], vacs[0]
    planted = [
        chunk
        for chunk in chunks
        if int(chunk["foff"]) >= int(jump["foff"])
        and int(chunk["foff"]) < int(vac["foff"])
    ]
    assert planted and all(chunk["kind"] == "lzma" for chunk in planted)
    assert sum(int(chunk["olen"]) for chunk in planted) == jump_y
    _assert_gadget_slip_is_minus_three(layout)


@pytest.mark.skipif(
    os.environ.get("QUINEPACK_SLOW_STORE") != "1",
    reason="set QUINEPACK_SLOW_STORE=1 to run the 48 MiB store build",
)
def test_large_store_roundtrip_48mib() -> None:
    data = os.urandom(48 * 1024 * 1024)
    entry = _file("large.bin", data)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="large.store.7z",
        seed_name=".quinepack/large.store.7z.seed",
        compressed=False,
    )

    verify_built(result, payload)
    assert int(result.layout["ds"]) > 64
    assert len(result.data) >= len(data)


def test_store_accepts_footer_at_chunk_limit() -> None:
    entries = tuple(_file(f"f{index:05d}.txt", b"x") for index in range(1660))
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="many.store.7z",
        seed_name=".quinepack/many.store.7z.seed",
        compressed=False,
    )

    verify_built(result, payload)
    assert int(result.layout["T"]) <= CHUNK


@pytest.mark.parametrize("count", (2000, 5000))
def test_store_accepts_large_footer(count: int) -> None:
    entries = tuple(_file(f"f{index:05d}.txt", b"x") for index in range(count))
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="many.store.7z",
        seed_name=".quinepack/many.store.7z.seed",
        compressed=False,
    )

    verify_built(result, payload)
    assert int(result.layout["T"]) > CHUNK

    if SEVEN_ZIP is not None:
        scratch = REPO_ROOT / "_tmp"
        scratch.mkdir(exist_ok=True)
        archive = Path(tempfile.mkdtemp(prefix="sevenzip-many-", dir=scratch))
        try:
            path = archive / "many.store.7z"
            path.write_bytes(result.data)
            completed = subprocess.run(
                [str(SEVEN_ZIP), "t", str(path)],
                cwd=archive,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
        finally:
            shutil.rmtree(archive, ignore_errors=True)


def test_store_large_footer_with_payload() -> None:
    entries = tuple(_file(f"f{index:05d}.txt", b"x") for index in range(2000))
    entries += (_file("big.bin", b"abcd" * (64 * 1024)),)
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="manybig.store.7z",
        seed_name=".quinepack/manybig.store.7z.seed",
        compressed=False,
    )

    verify_built(result, payload)
    assert int(result.layout["T"]) > CHUNK


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