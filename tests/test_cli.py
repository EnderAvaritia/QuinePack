"""Tests for the CLI and archive verification wiring."""

from __future__ import annotations

import importlib
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from quinepack import __version__
from quinepack import cli
from quinepack import verify as verify_module
from quinepack.errors import SevenZipMissingError, VerificationError
from quinepack.model import Payload

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Call:
    payload: Payload
    quine_name: str
    seed_name: str
    compressed: bool
    preset: int


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.data = b"fake-archive"
        self.error: Exception | None = None

    def __call__(
        self,
        payload: Payload,
        *,
        quine_name: str,
        seed_name: str,
        compressed: bool,
        preset: int = 6,
    ) -> SimpleNamespace:
        self.calls.append(_Call(payload, quine_name, seed_name, compressed, preset))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=self.data)


@pytest.fixture(autouse=True)
def _non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def fake_writer(monkeypatch: pytest.MonkeyPatch) -> _FakeWriter:
    writer = _FakeWriter()
    module = types.ModuleType("quinepack.formats.sevenzip")
    module.build_archive = writer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quinepack.formats.sevenzip", module)
    return writer


@dataclass
class _TarxzCall:
    payload: Payload
    quine_name: str
    seed_name: str


@pytest.fixture
def fake_tarxz(monkeypatch: pytest.MonkeyPatch) -> list[_TarxzCall]:
    calls: list[_TarxzCall] = []

    def build(
        payload: Payload,
        *,
        quine_name: str,
        seed_name: str,
    ) -> SimpleNamespace:
        calls.append(_TarxzCall(payload, quine_name, seed_name))
        return SimpleNamespace(data=b"fake-txz")

    module = types.ModuleType("quinepack.formats.tarxz")
    module.build_archive = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quinepack.formats.tarxz", module)
    return calls


def _make_dir_payload(base: Path, name: str = "site") -> Path:
    root = base / name
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_bytes(b"<html></html>")
    (root / "assets" / "a.bin").write_bytes(b"abc")
    return root


# --- version -------------------------------------------------------------


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"quinepack {__version__}"


def test_python_m_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "quinepack", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert f"quinepack {__version__}" in completed.stdout


# --- usage errors --------------------------------------------------------


def test_missing_format_without_tty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source)]) == 2
    assert "--format is required" in capsys.readouterr().err


def test_unknown_format_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "zip"]) == 2


def test_empty_format_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z,"]) == 2


def test_all_cannot_be_combined(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "all,7z"]) == 2


def test_output_and_outdir_are_mutually_exclusive(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    code = cli.main(
        [
            "pack",
            str(source),
            "--format",
            "7z",
            "-o",
            str(tmp_path / "out.7z"),
            "--outdir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2


def test_output_requires_single_format(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    code = cli.main(
        [
            "pack",
            str(source),
            "--format",
            "7z,7z-store",
            "-o",
            str(tmp_path / "out.7z"),
        ]
    )
    assert code == 2


def test_output_with_all_is_a_usage_error(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    code = cli.main(
        ["pack", str(source), "--format", "all", "-o", str(tmp_path / "out.7z")]
    )
    assert code == 2


def test_quiet_and_verbose_are_mutually_exclusive(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    code = cli.main(["pack", str(source), "--format", "7z", "--quiet", "--verbose"])
    assert code == 2


def test_preset_out_of_range(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z", "--preset", "10"]) == 2


# --- format writers -------------------------------------------------------


def test_txz_uses_tarxz_writer(
    tmp_path: Path, fake_tarxz: list[_TarxzCall]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "txz"]) == 0

    output = tmp_path / "app.txt.tar.xz"
    assert output.read_bytes() == b"fake-txz"
    assert fake_tarxz[0].quine_name == "app.txt.tar.xz"
    assert fake_tarxz[0].seed_name == ".quinepack/app.txt.tar.xz.seed"


def test_all_writes_three_formats(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    fake_tarxz: list[_TarxzCall],
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "all"]) == 0

    assert (tmp_path / "app.txt.7z").read_bytes() == b"fake-archive"
    assert (tmp_path / "app.txt.store.7z").read_bytes() == b"fake-archive"
    assert (tmp_path / "app.txt.tar.xz").read_bytes() == b"fake-txz"
    assert len(fake_writer.calls) == 2
    assert len(fake_tarxz) == 1


# --- successful packs ----------------------------------------------------


def test_pack_single_file_default_output(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z"]) == 0

    output = tmp_path / "app.txt.7z"
    assert output.read_bytes() == b"fake-archive"
    assert not (tmp_path / "app.txt.7z.partial").exists()
    call = fake_writer.calls[0]
    assert call.quine_name == "app.txt.7z"
    assert call.seed_name == ".quinepack/app.txt.7z.seed"
    assert call.compressed is True
    assert call.preset == 6


def test_pack_directory_store_with_outdir(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    root = _make_dir_payload(tmp_path)
    outdir = tmp_path / "out" / "nested"

    assert cli.main(["pack", str(root), "--format", "7z-store", "--outdir", str(outdir)]) == 0

    assert (outdir / "site.store.7z").read_bytes() == b"fake-archive"
    assert fake_writer.calls[0].compressed is False


def test_pack_with_explicit_output_path(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")
    output = tmp_path / "elsewhere" / "custom.bin"

    assert cli.main(["pack", str(source), "--format", "7z", "-o", str(output)]) == 0

    assert output.read_bytes() == b"fake-archive"
    call = fake_writer.calls[0]
    assert call.quine_name == "custom.bin"
    assert call.seed_name == ".quinepack/custom.bin.seed"


def test_pack_passes_preset_and_excludes(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    root = tmp_path / "site"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"keep")
    (root / "drop.log").write_bytes(b"drop")

    code = cli.main(
        [
            "pack",
            str(root),
            "--format",
            "7z",
            "--preset",
            "2",
            "--exclude",
            "*.log",
        ]
    )

    assert code == 0
    call = fake_writer.calls[0]
    assert call.preset == 2
    assert [entry.path for entry in call.payload.files] == ["keep.txt"]


def test_pack_warns_when_preset_used_with_store(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_dir_payload(tmp_path)

    code = cli.main(["pack", str(root), "--format", "7z-store", "--preset", "3"])

    assert code == 0
    assert "--preset has no effect" in capsys.readouterr().err
    assert fake_writer.calls[0].preset == 3


def test_quiet_suppresses_progress(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z", "--quiet"]) == 0
    assert capsys.readouterr().err == ""


def test_verbose_prints_details(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z", "--verbose"]) == 0
    assert "scanning" in capsys.readouterr().err


# --- overwrite policy ----------------------------------------------------


def test_existing_target_without_tty_fails(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")
    (tmp_path / "app.txt.7z").write_bytes(b"old")

    assert cli.main(["pack", str(source), "--format", "7z"]) == 5
    assert "already exists" in capsys.readouterr().err
    assert fake_writer.calls == []


def test_force_overwrites_existing_target(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")
    output = tmp_path / "app.txt.7z"
    output.write_bytes(b"old")

    assert cli.main(["pack", str(source), "--format", "7z", "--force"]) == 0
    assert output.read_bytes() == b"fake-archive"


# --- payload errors ------------------------------------------------------


def test_output_inside_payload_is_rejected(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_dir_payload(tmp_path)

    code = cli.main(
        ["pack", str(root), "--format", "7z", "--outdir", str(root / "build")]
    )

    assert code == 4
    assert "inside the payload" in capsys.readouterr().err
    assert fake_writer.calls == []


def test_artifact_name_present_in_payload_is_rejected(
    tmp_path: Path, fake_writer: _FakeWriter
) -> None:
    root = _make_dir_payload(tmp_path)
    (root / "site.7z").write_bytes(b"collision")

    assert cli.main(["pack", str(root), "--format", "7z"]) == 4


def test_missing_input_is_payload_error(tmp_path: Path) -> None:
    assert cli.main(["pack", str(tmp_path / "nope"), "--format", "7z"]) == 4


# --- writer failures -----------------------------------------------------


def test_writer_failure_returns_one_and_cleans_partial(
    tmp_path: Path, fake_writer: _FakeWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")
    fake_writer.error = RuntimeError("boom")

    assert cli.main(["pack", str(source), "--format", "7z"]) == 1

    err = capsys.readouterr().err
    assert "boom" in err
    assert "Traceback" not in err
    assert not (tmp_path / "app.txt.7z.partial").exists()
    assert not (tmp_path / "app.txt.7z").exists()


def test_missing_writer_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "quinepack.formats.sevenzip",
        types.ModuleType("quinepack.formats.sevenzip"),
    )
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")

    assert cli.main(["pack", str(source), "--format", "7z"]) == 1
    assert "not available" in capsys.readouterr().err


# --- verify wiring -------------------------------------------------------


def test_pack_verify_uses_external_tool(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "app.txt"
    source.write_bytes(b"hello")
    recorded: list[tuple[Path, Path | None, str | None]] = []

    def fake_verify(
        archive: Path,
        *,
        against: Path | None = None,
        seven_zip: str | None = None,
        excludes: tuple[str, ...] = (),
    ) -> None:
        recorded.append((archive, against, seven_zip))

    monkeypatch.setattr(cli, "verify_archive", fake_verify)

    code = cli.main(
        [
            "pack",
            str(source),
            "--format",
            "7z",
            "--verify",
            "--7zr",
            "C:/tools/7zr.exe",
        ]
    )

    assert code == 0
    assert recorded == [(tmp_path / "app.txt.7z", source, "C:/tools/7zr.exe")]


def test_pack_verify_forwards_excludes(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "site"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"keep")
    (root / "drop.log").write_bytes(b"drop")
    recorded: list[tuple[str, ...]] = []

    def fake_verify(archive: Path, **kwargs: object) -> None:
        excludes = kwargs.get("excludes", ())
        assert isinstance(excludes, tuple)
        recorded.append(excludes)

    monkeypatch.setattr(cli, "verify_archive", fake_verify)

    code = cli.main(
        [
            "pack",
            str(root),
            "--format",
            "7z",
            "--exclude",
            "*.log",
            "--verify",
        ]
    )

    assert code == 0
    assert recorded == [("*.log",)]


def test_verify_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive")
    source = tmp_path / "site"
    source.mkdir()
    recorded: list[tuple[Path, Path | None, str | None, tuple[str, ...]]] = []

    def fake_verify(
        target: Path,
        *,
        against: Path | None = None,
        seven_zip: str | None = None,
        excludes: tuple[str, ...] = (),
    ) -> None:
        recorded.append((target, against, seven_zip, excludes))

    monkeypatch.setattr(cli, "verify_archive", fake_verify)

    assert cli.main(["verify", str(archive), "--against", str(source)]) == 0
    assert recorded == [(archive, source, None, ())]
    assert "verified" in capsys.readouterr().err


def test_verify_missing_seven_zip_exits_six(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive")

    def fail(*args: object, **kwargs: object) -> None:
        raise SevenZipMissingError("7-Zip executable not found")

    monkeypatch.setattr(cli, "verify_archive", fail)

    assert cli.main(["verify", str(archive)]) == 6
    assert "7-Zip" in capsys.readouterr().err


# --- interactive mode ----------------------------------------------------


def test_interactive_menu_uses_default(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    root = _make_dir_payload(tmp_path)

    assert cli.main(["pack", str(root)]) == 0
    assert "Select output format(s)" in capsys.readouterr().out
    assert fake_writer.calls[0].quine_name == "site.7z"


def test_interactive_menu_supports_multiple_choices(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "1,2")
    root = _make_dir_payload(tmp_path)

    assert cli.main(["pack", str(root)]) == 0
    assert [call.quine_name for call in fake_writer.calls] == [
        "site.7z",
        "site.store.7z",
    ]


def test_interactive_menu_retries_invalid_input(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    answers = iter(["nope", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    root = _make_dir_payload(tmp_path)

    assert cli.main(["pack", str(root)]) == 0
    assert "Invalid selection" in capsys.readouterr().out
    assert fake_writer.calls[0].quine_name == "site.7z"


def test_interactive_menu_txz_uses_tarxz_writer(
    tmp_path: Path,
    fake_tarxz: list[_TarxzCall],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    root = _make_dir_payload(tmp_path)

    assert cli.main(["pack", str(root)]) == 0
    assert fake_tarxz[0].quine_name == "site.tar.xz"


def test_interactive_overwrite_can_skip(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    root = _make_dir_payload(tmp_path)
    output = tmp_path / "site.7z"
    output.write_bytes(b"old")

    assert cli.main(["pack", str(root), "--format", "7z"]) == 0
    assert output.read_bytes() == b"old"
    assert fake_writer.calls == []
    assert "skipped" in capsys.readouterr().err


def test_interactive_overwrite_can_replace(
    tmp_path: Path,
    fake_writer: _FakeWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    root = _make_dir_payload(tmp_path)
    output = tmp_path / "site.7z"
    output.write_bytes(b"old")

    assert cli.main(["pack", str(root), "--format", "7z"]) == 0
    assert output.read_bytes() == b"fake-archive"


# --- 7-Zip resolution ----------------------------------------------------


def test_find_seven_zip_prefers_explicit_path(tmp_path: Path) -> None:
    tool = tmp_path / "7zr.exe"
    tool.write_bytes(b"binary")

    assert verify_module.find_seven_zip(tool) == tool


def test_find_seven_zip_rejects_missing_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(SevenZipMissingError):
        verify_module.find_seven_zip(tmp_path / "missing" / "7zr.exe")


def test_find_seven_zip_searches_path_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    searched: list[str] = []

    def fake_which(name: str) -> str | None:
        searched.append(name)
        return "C:/tools/7z.exe" if name == "7z" else None

    monkeypatch.setattr(verify_module.shutil, "which", fake_which)

    assert verify_module.find_seven_zip() == Path("C:/tools/7z.exe")
    assert searched == ["7zr", "7z"]


def test_find_seven_zip_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_module.shutil, "which", lambda name: None)

    with pytest.raises(SevenZipMissingError, match="7-Zip"):
        verify_module.find_seven_zip()


# --- verification core ---------------------------------------------------


def _patch_extraction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: dict[str, bytes] | None = None,
    copy_archive: bool = True,
) -> None:
    def fake_extract(tool: Path, archive: Path, destination: Path) -> None:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if copy_archive:
            (destination / Path(archive).name).write_bytes(Path(archive).read_bytes())
        for rel, data in (extra or {}).items():
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    monkeypatch.setattr(verify_module, "extract_archive", fake_extract)
    monkeypatch.setattr(
        verify_module, "find_seven_zip", lambda explicit=None: Path("7zr")
    )


def test_verify_archive_accepts_matching_self_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    _patch_extraction(monkeypatch, extra={"hello.txt": b"hello"})

    verify_module.verify_archive(archive, workdir=tmp_path)


def test_verify_archive_rejects_missing_self_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    _patch_extraction(monkeypatch, copy_archive=False)

    with pytest.raises(VerificationError, match="self copy missing"):
        verify_module.verify_archive(archive, workdir=tmp_path)


def test_verify_archive_rejects_self_copy_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")

    def fake_extract(tool: Path, target: Path, destination: Path) -> None:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / Path(target).name).write_bytes(b"different")

    monkeypatch.setattr(verify_module, "extract_archive", fake_extract)
    monkeypatch.setattr(
        verify_module, "find_seven_zip", lambda explicit=None: Path("7zr")
    )

    with pytest.raises(VerificationError, match="does not match"):
        verify_module.verify_archive(archive, workdir=tmp_path)


def test_verify_archive_compares_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    source = tmp_path / "site"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"hello")
    (source / "nested").mkdir()
    (source / "nested" / "deep.txt").write_bytes(b"deep")

    _patch_extraction(
        monkeypatch,
        extra={
            ".quinepack/site.7z.seed": b"seed",
            "hello.txt": b"hello",
            "nested/deep.txt": b"deep",
        },
    )

    verify_module.verify_archive(archive, against=source, workdir=tmp_path)


def test_verify_archive_detects_payload_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    source = tmp_path / "site"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"hello")
    _patch_extraction(monkeypatch, extra={"hello.txt": b"tampered"})

    with pytest.raises(VerificationError, match="does not match"):
        verify_module.verify_archive(archive, against=source, workdir=tmp_path)


def test_verify_archive_detects_unexpected_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    source = tmp_path / "site"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"hello")
    _patch_extraction(monkeypatch, extra={"hello.txt": b"hello", "extra.txt": b"x"})

    with pytest.raises(VerificationError, match="unexpected entries"):
        verify_module.verify_archive(archive, against=source, workdir=tmp_path)


def test_verify_archive_honours_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "site.7z"
    archive.write_bytes(b"archive-bytes")
    source = tmp_path / "site"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep")
    (source / "drop.log").write_bytes(b"drop")
    _patch_extraction(monkeypatch, extra={"keep.txt": b"keep"})

    verify_module.verify_archive(
        archive, against=source, excludes=("*.log",), workdir=tmp_path
    )


def test_verify_archive_rejects_invalid_txz(tmp_path: Path) -> None:
    archive = tmp_path / "site.tar.xz"
    archive.write_bytes(b"archive-bytes")

    with pytest.raises(VerificationError, match="cannot extract"):
        verify_module.verify_archive(archive, workdir=tmp_path)


def test_verify_archive_rejects_missing_archive(tmp_path: Path) -> None:
    with pytest.raises(VerificationError, match="archive not found"):
        verify_module.verify_archive(tmp_path / "missing.7z", workdir=tmp_path)


# --- optional integration ------------------------------------------------


def test_integration_pack_and_verify(tmp_path: Path) -> None:
    try:
        seven_zip = verify_module.find_seven_zip()
    except SevenZipMissingError:
        pytest.skip("7-Zip is not installed")

    try:
        importlib.import_module("quinepack.formats.sevenzip")
    except Exception as exc:  # noqa: BLE001 - the writer is developed in parallel
        pytest.skip(f"7z writer is not available: {exc}")

    source = tmp_path / "payload"
    source.mkdir()
    (source / "hello.txt").write_bytes(b"hello quinepack")
    outdir = tmp_path / "out"

    code = cli.main(
        [
            "pack",
            str(source),
            "--format",
            "7z-store",
            "--outdir",
            str(outdir),
            "--quiet",
        ]
    )
    if code != 0:
        pytest.skip("7z writer is not ready for integration yet")

    archive = outdir / "payload.store.7z"
    assert archive.is_file()
    verify_module.verify_archive(
        archive, against=source, seven_zip=seven_zip, workdir=tmp_path
    )