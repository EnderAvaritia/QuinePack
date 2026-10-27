"""End-to-end CLI tests against the real writer and external 7-Zip."""

from __future__ import annotations

from pathlib import Path

import pytest

from quinepack import cli

SEVEN_ZIP = Path(__file__).resolve().parents[1] / "_tmp" / "7zextra" / "x64" / "7za.exe"


pytestmark = pytest.mark.skipif(
    not SEVEN_ZIP.exists(), reason="official 7-Zip is not available"
)


def _make_site(root: Path) -> Path:
    site = root / "site"
    (site / "assets").mkdir(parents=True)
    (site / "assets" / "app.bin").write_bytes(bytes(range(256)) * 8)
    (site / "index.html").write_text("<h1>QuinePack</h1>\n", encoding="utf-8")
    (site / "empty").mkdir()
    return site


def test_pack_and_verify_both_7z_variants(tmp_path: Path) -> None:
    site = _make_site(tmp_path)
    outdir = tmp_path / "out"

    code = cli.main(
        [
            "pack",
            str(site),
            "--format",
            "7z,7z-store",
            "--outdir",
            str(outdir),
            "--7zr",
            str(SEVEN_ZIP),
            "--verify",
            "--quiet",
        ]
    )

    assert code == 0
    assert (outdir / "site.7z").is_file()
    assert (outdir / "site.store.7z").is_file()


def test_pack_is_deterministic(tmp_path: Path) -> None:
    site = _make_site(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    for outdir in (first, second):
        assert (
            cli.main(
                [
                    "pack",
                    str(site),
                    "--format",
                    "7z",
                    "--outdir",
                    str(outdir),
                    "--quiet",
                ]
            )
            == 0
        )

    assert (first / "site.7z").read_bytes() == (second / "site.7z").read_bytes()


def test_pack_single_file_and_verify_subcommand(tmp_path: Path) -> None:
    source = tmp_path / "app.txt"
    source.write_text("single file payload\n", encoding="utf-8")
    outdir = tmp_path / "out"

    assert (
        cli.main(
            [
                "pack",
                str(source),
                "--format",
                "7z",
                "--outdir",
                str(outdir),
                "--7zr",
                str(SEVEN_ZIP),
                "--verify",
                "--quiet",
            ]
        )
        == 0
    )

    archive = outdir / "app.txt.7z"
    assert (
        cli.main(
            [
                "verify",
                str(archive),
                "--against",
                str(source),
                "--7zr",
                str(SEVEN_ZIP),
                "--quiet",
            ]
        )
        == 0
    )

def test_verify_subcommand_honors_exclude(tmp_path: Path) -> None:
    site = _make_site(tmp_path)
    (site / "debug.tmp").write_text("drop me\n", encoding="utf-8")
    outdir = tmp_path / "out"

    assert (
        cli.main(
            [
                "pack",
                str(site),
                "--format",
                "7z",
                "--exclude",
                "*.tmp",
                "--outdir",
                str(outdir),
                "--quiet",
            ]
        )
        == 0
    )

    assert (
        cli.main(
            [
                "verify",
                str(outdir / "site.7z"),
                "--against",
                str(site),
                "--exclude",
                "*.tmp",
                "--7zr",
                str(SEVEN_ZIP),
                "--quiet",
            ]
        )
        == 0
    )
