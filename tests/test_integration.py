"""End-to-end CLI tests against the real writer and external 7-Zip."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from quinepack import cli
from quinepack.errors import SevenZipMissingError
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


pytestmark = pytest.mark.skipif(
    SEVEN_ZIP is None, reason="official 7-Zip is not available"
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


def test_size_gates_against_official_7z_and_plain_tar(tmp_path: Path) -> None:
    import tarfile

    site = _make_site(tmp_path)
    (site / "manual.txt").write_text(
        ("quinepack size gate " * 80 + "\n") * 120, encoding="utf-8"
    )
    outdir = tmp_path / "out"

    assert (
        cli.main(
            [
                "pack",
                str(site),
                "--format",
                "all",
                "--outdir",
                str(outdir),
                "--quiet",
            ]
        )
        == 0
    )

    plain_7z = tmp_path / "plain.7z"
    plain_store = tmp_path / "plain.store.7z"
    for target, extra in (
        (plain_7z, []),
        (plain_store, ["-mx=0"]),
    ):
        completed = subprocess.run(
            [str(SEVEN_ZIP), "a", "-t7z", *extra, str(target), "."],
            cwd=site,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    plain_tar = tmp_path / "plain.tar"
    with tarfile.open(plain_tar, "w") as archive:
        for path in sorted(site.rglob("*")):
            archive.add(path, arcname=path.relative_to(site).as_posix())

    def size(path: Path) -> int:
        return path.stat().st_size

    def allowance(base: int, percent: int, floor: int) -> int:
        return base + max(base * percent // 100, floor)

    assert size(outdir / "site.7z") <= allowance(size(plain_7z), 2, 256 << 10)
    assert size(outdir / "site.store.7z") <= allowance(
        size(plain_store), 5, 512 << 10
    )
    assert size(outdir / "site.tar.xz") <= allowance(
        size(plain_tar), 5, 512 << 10
    )


def test_pack_all_and_external_txz_acceptance(tmp_path: Path) -> None:
    xz = shutil.which("xz")
    tar = shutil.which("tar")
    if xz is None or tar is None:
        pytest.skip("xz and tar are not available")

    site = _make_site(tmp_path)
    outdir = tmp_path / "out"

    assert (
        cli.main(
            [
                "pack",
                str(site),
                "--format",
                "all",
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

    txz = outdir / "site.tar.xz"
    assert txz.is_file()
    assert subprocess.run([xz, "-t", str(txz)], check=False).returncode == 0
    assert (
        subprocess.run(
            [tar, "--force-local", "-tJf", str(txz)],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
