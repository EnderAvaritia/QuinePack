"""TXZ CLI acceptance that does not require external 7-Zip."""

from __future__ import annotations

from pathlib import Path

from quinepack import cli


def test_pack_and_verify_txz_without_seven_zip(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True)
    (site / "assets" / "app.bin").write_bytes(bytes(range(64)))
    (site / "index.html").write_text("<h1>QuinePack</h1>\n", encoding="utf-8")
    (site / "empty").mkdir()

    outdir = tmp_path / "out"
    assert (
        cli.main(
            [
                "pack",
                str(site),
                "--format",
                "txz",
                "--outdir",
                str(outdir),
                "--verify",
                "--quiet",
            ]
        )
        == 0
    )

    assert (outdir / "site.tar.xz").is_file()
    assert (
        cli.main(
            [
                "verify",
                str(outdir / "site.tar.xz"),
                "--against",
                str(site),
                "--quiet",
            ]
        )
        == 0
    )