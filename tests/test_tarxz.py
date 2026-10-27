"""Tests for the store-only tar.xz quine writer."""

from __future__ import annotations

import io
import lzma
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from quinepack.errors import PayloadError, VerificationError
from quinepack.formats.tarxz import (
    BuildResult,
    build_archive,
    verify_built,
)
from quinepack.model import Payload, PayloadEntry
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
        assert archive.getmember("文档/说明.txt").isfile()
        assert archive.getmember("文档").isdir()


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


def test_ustar_name_too_long_raises() -> None:
    entry = _file("x" * 120, b"data")
    payload = _payload((entry,), single_file=True)

    with pytest.raises(PayloadError, match="too long for POSIX ustar"):
        build_archive(
            payload,
            quine_name="long.tar.xz",
            seed_name=".quinepack/long.tar.xz.seed",
        )


def test_external_xz_and_tar_accept_archive() -> None:
    xz = shutil.which("xz")
    tar = shutil.which("tar")
    if xz is None or tar is None:
        pytest.skip("xz and tar executables are both required")

    payload = _payload(
        (
            _dir("docs"),
            _file("docs/readme.txt", b"quinepack external test\n" * 40, MTIME_B),
            _file("empty.txt", b"", MTIME_A),
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
