"""Tests for the store-only tar.xz quine writer."""

from __future__ import annotations

import io
import lzma
import os
import random
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import cast

import pytest

from quinepack.errors import PayloadError, VerificationError
from quinepack.formats.tarxz import (
    BuildResult,
    _TarXzBuilder,
    _add_gear,
    _crc64_parts_parallel,
    _crc64_parts_sequential,
    _crc64_update,
    _crc64_worker,
    _round_up_512,
    build_archive,
    verify_built,
)
from quinepack.lzma2 import CHUNK, LZ2_MAX_UNPACKED
from quinepack.model import Payload, PayloadEntry
from quinepack.parallel import TaskPool
from quinepack.verify import verify_archive

REPO_ROOT = Path(__file__).resolve().parents[1]
MTIME_A = 1_600_000_000_000_000_000
MTIME_B = 1_600_000_123_456_789_000
XZ_MAGIC = b"\xFD7zXZ\x00"


def _file(
    path: str,
    data: bytes,
    mtime_ns: int = MTIME_A,
    mode: int = 0o644,
) -> PayloadEntry:
    return PayloadEntry(path, False, data, mtime_ns, mode)


def _dir(
    path: str,
    mtime_ns: int = MTIME_A,
    mode: int = 0o755,
) -> PayloadEntry:
    return PayloadEntry(path, True, None, mtime_ns, mode)


def _payload(entries: tuple[PayloadEntry, ...], *, single_file: bool) -> Payload:
    return Payload(
        source=Path("input"),
        entries=entries,
        total_bytes=sum(len(entry.data or b"") for entry in entries),
        is_single_file=single_file,
    )


def _tar_members(result: BuildResult) -> tuple[tarfile.TarFile, io.BytesIO]:
    tar_data = lzma.decompress(result.data, format=lzma.FORMAT_XZ)
    buffer = io.BytesIO(tar_data)
    return tarfile.open(fileobj=buffer), buffer


def test_small_text_file_roundtrip() -> None:
    entry = _file("notes.txt", b"hello quinepack\n" * 64)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="notes.txt.tar.xz",
        seed_name=".quinepack/notes.txt.tar.xz.seed",
    )

    verify_built(result, payload)
    assert result.compressed is False
    assert result.data.startswith(XZ_MAGIC)
    assert result.entries == (
        (".quinepack/notes.txt.tar.xz.seed", False),
        ("notes.txt", False),
        ("notes.txt.tar.xz", False),
    )
    assert result.files == (("notes.txt", b"hello quinepack\n" * 64),)

    archive, _ = _tar_members(result)
    with archive:
        assert [member.name for member in archive.getmembers()] == [
            ".quinepack/notes.txt.tar.xz.seed",
            "notes.txt",
            "notes.txt.tar.xz",
        ]


def test_small_binary_file_roundtrip() -> None:
    data = bytes(range(256)) * 3 + b"\x00\xff" * 17
    entry = _file("blob.bin", data)
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="blob.bin.tar.xz",
        seed_name=".quinepack/blob.bin.tar.xz.seed",
    )

    verify_built(result, payload)
    assert result.files == (("blob.bin", data),)
    # Store-only: the archive carries the payload rather than compressing it.
    assert len(result.data) >= len(data)


def test_directory_nested_empty_dir_and_empty_file() -> None:
    entries = (
        _dir("assets", MTIME_A, 0o755),
        _dir("assets/empty", MTIME_B, 0o700),
        _file("assets/nested.bin", b"\xde\xad\xbe\xef" * 200, MTIME_B, 0o640),
        _file("empty.txt", b"", MTIME_A, 0o600),
        _file("index.txt", b"index\n" * 32, MTIME_B),
    )
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="site.tar.xz",
        seed_name=".quinepack/site.tar.xz.seed",
    )

    verify_built(result, payload)
    assert result.entries == (
        (".quinepack/site.tar.xz.seed", False),
        ("assets", True),
        ("assets/empty", True),
        ("assets/nested.bin", False),
        ("empty.txt", False),
        ("index.txt", False),
        ("site.tar.xz", False),
    )
    assert result.files == (
        ("assets/nested.bin", b"\xde\xad\xbe\xef" * 200),
        ("empty.txt", b""),
        ("index.txt", b"index\n" * 32),
    )

    archive, _ = _tar_members(result)
    with archive:
        for entry in payload.entries:
            member = archive.getmember(entry.path)
            assert member.mtime == entry.mtime_ns // 1_000_000_000
            expected_mode = entry.mode or (0o755 if entry.is_dir else 0o644)
            assert member.mode == expected_mode
        generated_mtime = entries[0].mtime_ns // 1_000_000_000
        assert archive.getmember(result.entries[0][0]).mtime == generated_mtime
        assert archive.getmember(result.entries[-1][0]).mtime == generated_mtime


def test_long_path_uses_ustar_prefix_field() -> None:
    long_name = "assets-" + "a" * 60 + "/" + "b" * 80 + ".bin"
    assert len(long_name.encode("utf-8")) > 100
    entry = _file(long_name, b"long path data")
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="long.tar.xz",
        seed_name=".quinepack/long.tar.xz.seed",
    )

    verify_built(result, payload)
    assert result.files == ((long_name, b"long path data"),)


def test_unicode_names_roundtrip() -> None:
    entries = (
        _dir("文档", MTIME_A),
        _file(
            "文档/说明.txt",
            "你好 quinepack".encode() * 8,
            MTIME_B,
        ),
    )
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="站点.tar.xz",
        seed_name=".quinepack/站点.tar.xz.seed",
    )

    verify_built(result, payload)
    archive, _ = _tar_members(result)
    with archive:
        member = archive.getmember("文档/说明.txt")
        assert member.isfile()
        assert member.pax_headers["path"] == "文档/说明.txt"
        assert archive.getmember("文档").isdir()


def test_crc64_parts_parallel_matches_sequential() -> None:
    parts = (
        bytes(range(256)) * 13,
        bytearray(range(251)) * 7,
        b"\x00" * 9,
    )
    expected = _crc64_parts_sequential(parts)
    with TaskPool(_crc64_worker, workers=2) as pool:
        assert _crc64_parts_parallel(parts, pool) == expected
    assert _crc64_parts_sequential((b"".join(parts),)) == expected


def test_crc64_update_matches_one_pass() -> None:
    data = bytes(range(256)) * 37
    split = _crc64_update(0, data[:313])
    assert _crc64_update(split, data[313:]) == _crc64_update(0, data)


def test_build_is_deterministic() -> None:
    entries = (
        _dir("a", MTIME_A),
        _file("a/one.txt", b"one" * 50, MTIME_A),
        _file("two.bin", b"\x01\x02\x03\x04" * 80, MTIME_B),
    )
    payload = _payload(entries, single_file=False)

    first = build_archive(
        payload,
        quine_name="same.tar.xz",
        seed_name=".quinepack/same.tar.xz.seed",
    )
    second = build_archive(
        payload,
        quine_name="same.tar.xz",
        seed_name=".quinepack/same.tar.xz.seed",
    )

    assert first.data == second.data
    assert first.seed == second.seed
    assert first.entries == second.entries


def test_long_name_uses_pax_header() -> None:
    long_name = "x" * 120
    entry = _file(long_name, b"data")
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="long.tar.xz",
        seed_name=".quinepack/long.tar.xz.seed",
    )

    verify_built(result, payload)
    archive, _ = _tar_members(result)
    with archive:
        member = archive.getmember(long_name)
        assert member.isfile()
        assert member.pax_headers["path"] == long_name
        assert archive.extractfile(long_name).read() == b"data"


def test_long_nested_name_uses_pax_header() -> None:
    long_name = "/".join(["a" * 90, "b" * 90, "c" * 90 + ".bin"])
    assert len(long_name.encode("utf-8")) > 255
    entry = _file(long_name, b"nested")
    payload = _payload((entry,), single_file=True)

    result = build_archive(
        payload,
        quine_name="long.compressed.tar.xz",
        seed_name=".quinepack/long.compressed.tar.xz.seed",
    )

    verify_built(result, payload)
    archive, _ = _tar_members(result)
    with archive:
        assert archive.getmember(long_name).isfile()


def test_long_artifact_and_seed_names_use_pax() -> None:
    artifact = "payload-" + "z" * 120 + ".tar.xz"
    seed = f".quinepack/{artifact}.seed"
    payload = _payload((_file("tiny.txt", b"tiny"),), single_file=True)

    result = build_archive(payload, quine_name=artifact, seed_name=seed)

    verify_built(result, payload)
    assert result.entries[0] == (seed, False)
    assert result.entries[-1] == (artifact, False)


def test_pax_needed_name_with_newline_is_rejected() -> None:
    entry = _file("x" * 120 + "\n", b"data")
    payload = _payload((entry,), single_file=True)

    with pytest.raises(PayloadError, match="PAX"):
        build_archive(
            payload,
            quine_name="long.tar.xz",
            seed_name=".quinepack/long.tar.xz.seed",
        )


def test_name_with_nul_is_rejected() -> None:
    entry = _file("bad\x00name", b"data")
    payload = _payload((entry,), single_file=True)

    with pytest.raises(PayloadError, match="NUL"):
        build_archive(
            payload,
            quine_name="bad.tar.xz",
            seed_name=".quinepack/bad.tar.xz.seed",
        )


def test_external_xz_and_tar_accept_archive() -> None:
    xz = shutil.which("xz")
    tar = shutil.which("tar")
    if xz is None or tar is None:
        pytest.skip("xz and tar executables are both required")

    long_name = "/".join(["long-" + "a" * 80, "b" * 90, "c" * 90 + ".txt"])

    payload = _payload(
        (
            _dir("docs"),
            _file("docs/readme.txt", b"quinepack external test\n" * 40, MTIME_B),
            _file("empty.txt", b"", MTIME_A),
            _file(long_name, b"pax long path\n", MTIME_A),
        ),
        single_file=False,
    )
    result = build_archive(
        payload,
        quine_name="site.tar.xz",
        seed_name=".quinepack/site.tar.xz.seed",
    )
    verify_built(result, payload)

    scratch_root = REPO_ROOT / "_tmp"
    scratch_root.mkdir(exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="tarxz-test-", dir=scratch_root))
    try:
        archive = workdir / "site.tar.xz"
        archive.write_bytes(result.data)

        xz_check = subprocess.run(
            [xz, "-t", str(archive)],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert xz_check.returncode == 0, (
            f"xz -t failed\nstdout:\n{xz_check.stdout}\nstderr:\n{xz_check.stderr}"
        )

        tar_check = subprocess.run(
            [tar, "-tJf", archive.name],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert tar_check.returncode == 0, (
            f"tar -tJf failed\nstdout:\n{tar_check.stdout}\nstderr:\n{tar_check.stderr}"
        )
        listed = set(tar_check.stdout.split())
        assert "site.tar.xz" in listed
        assert "docs/readme.txt" in listed
        assert long_name in listed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

def test_many_tiny_entries_select_a_large_enough_dictionary() -> None:
    entries = tuple(
        _file(f"tiny-{index:04}.txt", b"x", MTIME_A) for index in range(2000)
    )
    payload = _payload(entries, single_file=False)

    result = build_archive(
        payload,
        quine_name="many.tar.xz",
        seed_name=".quinepack/many.tar.xz.seed",
    )

    verify_built(result, payload)
    # The old len(content)-based dictionary bound corrupted this case.
    assert lzma.decompress(result.data, format=lzma.FORMAT_XZ)


class _SizedPayload:
    """Length-only payload stand-in for layout tests.

    ``_layout_iterate`` only reads entry sizes, so this avoids materialising a
    42 MiB buffer while still running the real layout code.
    """

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return True


def _layout_payload(size: int) -> Payload:
    entry = _file("payload.bin", cast(bytes, _SizedPayload(size)))
    return _payload((entry,), single_file=True)


def _layout_at(size: int) -> dict[str, object]:
    builder = _TarXzBuilder(
        _layout_payload(size),
        "payload.bin.tar.xz",
        ".quinepack/payload.bin.tar.xz.seed",
    )
    try:
        return builder._layout_iterate(64)
    finally:
        builder.close()


def test_gear_splits_slip_above_one_store_chunk() -> None:
    layout = _layout_at(42 << 20)
    q = int(layout["q"])
    chunks = layout["chunks"]

    start = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk["kind"] == "store" and not chunk.get("c1")
    )
    gear = chunks[start]
    slip = int(gear["foff"]) - (int(gear["ooff"]) - q)
    length = slip + 3
    # The old capped pair produced length == CHUNK here and shifted the echo.
    assert length > CHUNK
    assert int(gear["foff"]) + 3 == (int(gear["ooff"]) - q) + length

    pieces: list[int] = []
    output_pos = int(gear["ooff"])
    file_pos = int(gear["foff"])
    for chunk in chunks[start:]:
        if chunk["kind"] != "store":
            break
        assert int(chunk["ooff"]) == output_pos
        assert int(chunk["foff"]) == file_pos
        pieces.append(int(chunk["olen"]))
        output_pos += int(chunk["olen"])
        file_pos += 3 + int(chunk["olen"])

    assert len(pieces) == -(-length // CHUNK)
    assert pieces[0] == length - (len(pieces) - 1) * CHUNK
    assert sum(pieces) == length
    assert all(0 < piece <= CHUNK for piece in pieces)

    echo = chunks[start + len(pieces)]
    assert echo["kind"] == "lzma"
    assert int(echo["ooff"]) == int(gear["foff"]) + 3 + q
    assert int(echo["olen"]) == pieces[0]
    if len(pieces) > 1:
        shim = chunks[start + len(pieces) + 1]
        assert shim["kind"] == "lzma"
        assert int(shim["olen"]) == 3
        assert int(shim["foff"]) == int(echo["foff"]) + int(echo["size"])


def test_gear_bumps_a_one_byte_piece() -> None:
    q = 1_000_000
    output_pos = q + 1_000
    slip = CHUNK - 2  # slip + 3 == CHUNK + 1: the first piece would be one byte
    file_pos = 1_000 + slip
    chunks: list[dict[str, object]] = []

    _add_gear(chunks, output_pos, file_pos, q, header_a_offset=100)

    bump = chunks[0]
    assert bump["kind"] == "store"
    assert bump["olen"] == 1
    assert bump["size"] == 4
    tiled = [chunk["olen"] for chunk in chunks[1:] if chunk["kind"] == "store"]
    assert tiled == [4, CHUNK]
    echoes = [chunk["olen"] for chunk in chunks if chunk["kind"] == "lzma"]
    assert echoes == [4, 3, CHUNK]


def test_layout_chunk_limits_and_store_alignment() -> None:
    for size in (1 << 20, 42 << 20):
        layout = _layout_at(size)
        q = int(layout["q"])
        assert (
            q
            + _round_up_512(int(layout["xz_size"]))
            + 1024
            <= int(layout["dict_size"])
        )
        for chunk in layout["chunks"]:
            if chunk["kind"] == "store":
                assert 0 < int(chunk["olen"]) <= CHUNK
            elif chunk["kind"] == "lzma":
                assert 2 <= int(chunk["olen"]) <= LZ2_MAX_UNPACKED
            if (
                chunk["kind"] == "store"
                and not chunk.get("c1")
                and not chunk.get("vac")
                and not chunk.get("trailing")
            ):
                assert (
                    int(chunk["ooff"]) - q + int(chunk["olen"])
                    <= int(chunk["foff"]) + 3
                )


def test_fit_dictionary_raises_property_for_large_output() -> None:
    builder = _TarXzBuilder(
        _layout_payload(1),
        "payload.bin.tar.xz",
        ".quinepack/payload.bin.tar.xz.seed",
    )
    try:
        # A 1 GiB - 1 MiB layout: the old 2q hint picked a 2 GiB dictionary
        # while the jump distance was 2,148,816,485 bytes.
        q = 1_072_695_296
        xz_size = 1_076_124_084
        layout: dict[str, object] = {
            "q": q,
            "xz_size": xz_size,
            "dict_size": 1 << 31,
            "bh": 12,
            "block_hdr": b"\x00" * 12,
        }
        needed = q + _round_up_512(xz_size) + 1024
        assert needed > 1 << 31

        fitted = builder._fit_dictionary(layout)
        assert int(fitted["dict_size"]) >= needed
        assert len(fitted["block_hdr"]) == 12
        assert fitted["block_hdr"] != layout["block_hdr"]

        small: dict[str, object] = {
            "q": 1_000,
            "xz_size": 2_000,
            "dict_size": 1 << 20,
            "bh": 12,
            "block_hdr": b"\x00" * 12,
        }
        assert builder._fit_dictionary(small) is small
    finally:
        builder.close()


@pytest.mark.skipif(
    not os.environ.get("QUINEPACK_SLOW_TESTS"),
    reason="set QUINEPACK_SLOW_TESTS=1 to run the 42 MiB regression",
)
def test_42_mib_layout_roundtrip() -> None:
    data = random.Random(20261028).randbytes(42 << 20)
    payload = _payload((_file("payload.bin", data),), single_file=True)

    result = build_archive(
        payload,
        quine_name="payload.bin.tar.xz",
        seed_name=".quinepack/payload.bin.tar.xz.seed",
    )
    verify_built(result, payload)

    q = int(result.layout["q"])
    assert q == 44_042_240
    assert int(result.layout["k"]) == 673
    tar_data = lzma.decompress(result.data, format=lzma.FORMAT_XZ)
    assert tar_data[q : q + len(result.data)] == result.data


def test_truncated_txz_is_a_verification_error(tmp_path: Path) -> None:
    payload = _payload((_file("tiny.txt", b"x" * 64),), single_file=True)
    result = build_archive(
        payload,
        quine_name="truncated.tar.xz",
        seed_name=".quinepack/truncated.tar.xz.seed",
    )
    archive = tmp_path / "truncated.tar.xz"
    archive.write_bytes(result.data[: len(result.data) // 2])

    with pytest.raises(VerificationError, match="cannot extract"):
        verify_archive(archive, workdir=tmp_path)
