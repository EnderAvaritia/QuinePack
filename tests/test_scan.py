"""Tests for payload scanning."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quinepack import scan
from quinepack.errors import PayloadError
from quinepack.scan import scan_payload


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_tree(root: Path) -> None:
    _write(root / "index.html", b"<html></html>")
    _write(root / "z.txt", b"last")
    _write(root / "assets" / "a.png", b"png")
    _write(root / "assets" / "nested" / "x.txt", b"deep")
    (root / "empty").mkdir(parents=True)


def test_single_file_payload(tmp_path: Path) -> None:
    source = _write(tmp_path / "app.txt", b"payload")
    payload = scan_payload(source)

    assert payload.is_single_file is True
    assert payload.source == source
    assert payload.total_bytes == 7
    assert [entry.path for entry in payload.entries] == ["app.txt"]
    entry = payload.entries[0]
    assert entry.is_dir is False
    assert entry.data == b"payload"
    info = os.stat(source)
    assert entry.mtime_ns == info.st_mtime_ns
    assert entry.mode == stat.S_IMODE(info.st_mode)


def test_directory_entries_are_ordered_directories_first(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _make_tree(root)

    payload = scan_payload(root)

    assert payload.is_single_file is False
    assert [entry.path for entry in payload.entries] == [
        "assets",
        "empty",
        "index.html",
        "z.txt",
        "assets/nested",
        "assets/a.png",
        "assets/nested/x.txt",
    ]
    assert payload.total_bytes == len(b"<html></html>") + len(b"last") + len(b"png") + len(b"deep")


def test_entries_are_marked_as_files_and_directories(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _make_tree(root)

    payload = scan_payload(root)

    dirs = {entry.path for entry in payload.dirs}
    files = {entry.path for entry in payload.files}
    assert dirs == {"assets", "empty", "assets/nested"}
    assert files == {"index.html", "z.txt", "assets/a.png", "assets/nested/x.txt"}
    assert all(entry.data is None for entry in payload.dirs)


def test_scan_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _make_tree(root)

    first = scan_payload(root)
    second = scan_payload(root)

    assert first == second


def test_hidden_files_are_included(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / ".env", b"SECRET=1")
    _write(root / ".git" / "config", b"[core]")

    payload = scan_payload(root)

    assert [entry.path for entry in payload.entries] == [".git", ".env", ".git/config"]


def test_empty_directories_are_included(tmp_path: Path) -> None:
    root = tmp_path / "site"
    (root / "empty" / "nested").mkdir(parents=True)

    payload = scan_payload(root)

    assert [entry.path for entry in payload.dirs] == ["empty", "empty/nested"]


def test_mtime_is_preserved_for_files_and_directories(tmp_path: Path) -> None:
    root = tmp_path / "site"
    source = _write(root / "file.txt", b"data")
    directory = root / "sub"
    directory.mkdir()
    stamp = 1_600_000_000_000_000_000
    os.utime(source, ns=(stamp, stamp))
    os.utime(directory, ns=(stamp, stamp))

    payload = scan_payload(root)
    by_path = {entry.path: entry for entry in payload.entries}

    assert by_path["file.txt"].mtime_ns == os.stat(source).st_mtime_ns
    assert by_path["sub"].mtime_ns == os.stat(directory).st_mtime_ns


def test_exclude_file_glob(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / "keep.txt", b"keep")
    _write(root / "skip.tmp", b"skip")

    payload = scan_payload(root, excludes=("*.tmp",))

    assert [entry.path for entry in payload.entries] == ["keep.txt"]


def test_exclude_prunes_directory_subtree(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / "keep.txt", b"keep")
    _write(root / "vendor" / "vendor.txt", b"vendor")
    _write(root / "vendor" / "nested" / "more.txt", b"more")

    payload = scan_payload(root, excludes=("vendor",))

    assert [entry.path for entry in payload.entries] == ["keep.txt"]


def test_exclude_matches_nested_paths(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / "logs" / "a.log", b"a")
    _write(root / "logs" / "b.txt", b"b")

    payload = scan_payload(root, excludes=("*.log",))

    assert [entry.path for entry in payload.entries] == ["logs", "logs/b.txt"]


def test_exclude_accepts_trailing_separator_and_dot_prefix(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / "cache" / "item.bin", b"x")
    _write(root / "keep.txt", b"keep")

    payload = scan_payload(root, excludes=("./cache/",))

    assert [entry.path for entry in payload.entries] == ["keep.txt"]


def test_single_file_change_while_reading_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "app.txt", b"payload")
    original = scan._stream_file_into

    def changing_stream(
        path: Path, label: str, expected_size: int, store: scan.ContentStore
    ) -> None:
        original(path, label, expected_size, store)
        os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)

    monkeypatch.setattr(scan, "_stream_file_into", changing_stream)

    with pytest.raises(PayloadError, match="changed while reading"):
        scan_payload(source)


def test_missing_input_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PayloadError, match="does not exist"):
        scan_payload(tmp_path / "missing")


def test_reserved_seed_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "site"
    (root / ".quinepack").mkdir(parents=True)
    _write(root / "index.html", b"x")

    with pytest.raises(PayloadError, match="\\.quinepack"):
        scan_payload(root)


def test_oversized_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "MAX_PAYLOAD_BYTES", 8)
    source = _write(tmp_path / "big.bin", b"0123456789")

    with pytest.raises(PayloadError, match="exceeds"):
        scan_payload(source)


def test_oversized_total_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "MAX_PAYLOAD_BYTES", 8)
    root = tmp_path / "site"
    _write(root / "a.bin", b"01234")
    _write(root / "b.bin", b"56789")

    with pytest.raises(PayloadError, match="exceeds"):
        scan_payload(root)


def test_symlink_to_outside_file_is_followed(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside" / "target.txt", b"target")
    root = tmp_path / "site"
    root.mkdir()
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is not permitted on this platform")

    payload = scan_payload(root)

    assert [entry.path for entry in payload.files] == ["link.txt"]
    assert payload.files[0].data == b"target"


def test_symlink_directory_cycle_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "site"
    nested = root / "nested"
    nested.mkdir(parents=True)
    _write(nested / "file.txt", b"x")
    try:
        os.symlink(root, nested / "loop", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this platform")

    with pytest.raises(PayloadError, match="cycle"):
        scan_payload(root)

def test_casefold_collisions_warn_without_renaming() -> None:
    warnings: list[str] = []
    index = scan._CaseFoldIndex(warnings.append)

    index.note("Docs/Readme.md")
    index.note("docs/readme.md")
    index.note("DOCS/README.MD")
    index.note("other.txt")

    assert len(warnings) == 2
    assert "Docs/Readme.md" in warnings[0]
    assert "docs/readme.md" in warnings[0]
    assert "DOCS/README.MD" in warnings[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only case folding")
def test_exclude_is_case_insensitive_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _write(root / "keep.TXT", b"keep")
    _write(root / "skip.TMP", b"skip")

    payload = scan_payload(root, excludes=("*.tmp",))

    assert [entry.path for entry in payload.entries] == ["keep.TXT"]
