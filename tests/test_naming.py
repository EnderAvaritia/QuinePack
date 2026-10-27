"""Tests for artifact naming and layout safety helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quinepack.errors import PayloadError
from quinepack.model import Payload, PayloadEntry
from quinepack.naming import (
    artifact_name,
    ensure_artifact_not_in_payload,
    ensure_output_not_inside_payload,
    format_from_artifact_name,
    format_is_compressed,
    seed_name,
)


def _payload(source: Path, paths: tuple[str, ...], *, single_file: bool = False) -> Payload:
    entries = tuple(
        PayloadEntry(path=path, is_dir=False, data=b"", mtime_ns=0, mode=0o644)
        for path in paths
    )
    return Payload(source=source, entries=entries, total_bytes=0, is_single_file=single_file)


@pytest.mark.parametrize(
    ("format_key", "expected"),
    [
        ("7z", "site.7z"),
        ("7z-store", "site.store.7z"),
        ("txz", "site.tar.xz"),
    ],
)
def test_directory_artifact_names(tmp_path: Path, format_key: str, expected: str) -> None:
    assert artifact_name(tmp_path / "site", format_key) == expected


@pytest.mark.parametrize(
    ("format_key", "expected"),
    [
        ("7z", "app.exe.7z"),
        ("7z-store", "app.exe.store.7z"),
        ("txz", "app.exe.tar.xz"),
    ],
)
def test_single_file_artifact_names(tmp_path: Path, format_key: str, expected: str) -> None:
    assert artifact_name(tmp_path / "app.exe", format_key) == expected


def test_trailing_separator_does_not_change_directory_name(tmp_path: Path) -> None:
    assert artifact_name(Path(str(tmp_path / "site") + "/"), "7z") == "site.7z"


def test_dot_input_uses_resolved_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert artifact_name(Path("."), "7z") == f"{tmp_path.name}.7z"


def test_unknown_format_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        artifact_name(tmp_path / "site", "zip")


def test_seed_name_lives_under_reserved_directory() -> None:
    assert seed_name("site.7z") == ".quinepack/site.7z.seed"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("site.7z", "7z"),
        ("site.store.7z", "7z-store"),
        ("site.tar.xz", "txz"),
        ("SITE.STORE.7Z", "7z-store"),
        ("notes.txt", None),
        ("site.xz", None),
    ],
)
def test_format_detection_from_artifact_name(name: str, expected: str | None) -> None:
    assert format_from_artifact_name(name) == expected


def test_only_7z_is_compressed() -> None:
    assert format_is_compressed("7z") is True
    assert format_is_compressed("7z-store") is False
    assert format_is_compressed("txz") is False


def test_artifact_name_conflict_is_rejected(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "site", ("index.html", "site.7z"))
    with pytest.raises(PayloadError, match="site.7z"):
        ensure_artifact_not_in_payload(payload, "site.7z")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only case folding")
def test_artifact_name_conflict_ignores_case_on_windows(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "site", ("SITE.7Z",))
    with pytest.raises(PayloadError, match="site.7z"):
        ensure_artifact_not_in_payload(payload, "site.7z")


def test_artifact_name_absent_is_accepted(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "site", ("index.html", "assets"))
    ensure_artifact_not_in_payload(payload, "site.7z")


def test_output_inside_payload_directory_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "site"
    source.mkdir()
    payload = _payload(source, ("index.html",))
    with pytest.raises(PayloadError, match="inside the payload"):
        ensure_output_not_inside_payload(payload, source / "site.7z")


def test_output_equal_to_payload_directory_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "site"
    source.mkdir()
    payload = _payload(source, ("index.html",))
    with pytest.raises(PayloadError, match="inside the payload"):
        ensure_output_not_inside_payload(payload, source)


def test_sibling_output_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "site"
    source.mkdir()
    payload = _payload(source, ("index.html",))
    ensure_output_not_inside_payload(payload, tmp_path / "site.7z")


def test_single_file_output_may_not_overwrite_payload(tmp_path: Path) -> None:
    source = tmp_path / "app.exe"
    payload = _payload(source, ("app.exe",), single_file=True)
    with pytest.raises(PayloadError, match="overwrite the payload"):
        ensure_output_not_inside_payload(payload, source)


def test_single_file_output_sibling_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "app.exe"
    payload = _payload(source, ("app.exe",), single_file=True)
    ensure_output_not_inside_payload(payload, tmp_path / "app.exe.7z")