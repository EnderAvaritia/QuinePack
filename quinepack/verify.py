"""Archive extraction and self-copy/payload verification (product spec 6.6)."""

from __future__ import annotations

import hashlib
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .errors import SevenZipMissingError, VerificationError
from .naming import SEED_DIR, format_from_artifact_name
from .scan import scan_payload

SEVEN_ZIP_CANDIDATES: tuple[str, ...] = ("7zr", "7z", "7z.exe", "7zr.exe")
_XZ_MAGIC = b"\xfd7zXZ\x00"
_CHUNK_SIZE = 1 << 20


def find_seven_zip(explicit: str | Path | None = None) -> Path:
    """Resolve the 7-Zip executable from an explicit path or PATH."""

    if explicit is not None and str(explicit):
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
        located = shutil.which(str(explicit))
        if located is not None:
            return Path(located)
        raise SevenZipMissingError(f"7-Zip executable not found: {explicit}")

    for name in SEVEN_ZIP_CANDIDATES:
        located = shutil.which(name)
        if located is not None:
            return Path(located)
    for candidate in _standard_install_candidates():
        if candidate.is_file():
            return candidate
    raise SevenZipMissingError(
        "7-Zip executable not found; install 7-Zip or pass --7zr PATH"
    )


def _standard_install_candidates() -> tuple[Path, ...]:
    """Common 7-Zip install locations on Windows, after the PATH lookup."""
    if os.name != "nt":
        return ()
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    ]
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(root) / "7-Zip" / "7z.exe" for root in roots if root
    ]
    if local:
        candidates.append(Path(local) / "Programs" / "7-Zip" / "7z.exe")
    return tuple(candidates)


def extract_archive(seven_zip: Path, archive: Path, destination: Path) -> None:
    """Extract an archive with the external 7-Zip executable."""

    destination.mkdir(parents=True, exist_ok=True)
    command = [str(seven_zip), "x", "-y", f"-o{destination}", str(archive)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise SevenZipMissingError(f"7-Zip executable not found: {seven_zip}") from exc
    except OSError as exc:
        raise VerificationError(f"cannot run 7-Zip ({seven_zip}): {exc}") from exc

    if completed.returncode != 0:
        raise VerificationError(_extraction_failure(archive, completed))


def verify_archive(
    archive: Path,
    *,
    against: Path | None = None,
    seven_zip: str | Path | None = None,
    workdir: Path | None = None,
    excludes: Sequence[str] = (),
) -> None:
    """Extract an archive and check the self copy, plus an optional payload."""

    archive_path = Path(archive)
    if not archive_path.is_file():
        raise VerificationError(f"archive not found: {archive_path}")

    format_key = format_from_artifact_name(archive_path.name)
    if format_key is None and _has_xz_magic(archive_path):
        format_key = "txz"
    base = Path(workdir) if workdir is not None else archive_path.parent
    base.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".quinepack-verify-", dir=base) as tmp:
        extracted = Path(tmp) / "payload"
        if format_key == "txz":
            _extract_txz(archive_path, extracted)
        else:
            tool = find_seven_zip(seven_zip)
            extract_archive(tool, archive_path, extracted)
        _verify_self_copy(archive_path, extracted)
        if against is not None:
            _verify_payload(Path(against), extracted, archive_path.name, excludes)


def _has_xz_magic(archive: Path) -> bool:
    try:
        with open(archive, "rb") as handle:
            return handle.read(len(_XZ_MAGIC)) == _XZ_MAGIC
    except OSError:
        return False


def _extract_txz(archive: Path, destination: Path) -> None:
    """Extract a standard tar.xz stream without an external tool."""

    destination.mkdir(parents=True, exist_ok=True)
    try:
        with lzma.open(archive, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:") as tar:
                _safe_extract(tar, destination)
    except (OSError, EOFError, ValueError, lzma.LZMAError, tarfile.TarError) as exc:
        raise VerificationError(f"cannot extract {archive.name}: {exc}") from exc


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    # Python 3.12+ warns unless an extraction filter is chosen; the checks
    # below already reject absolute paths and links out of the destination.
    extract_kwargs = (
        {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
    )
    for member in tar.getmembers():
        if not (member.isreg() or member.isdir()):
            raise VerificationError(
                f"archive contains an unsupported entry type: {member.name}"
            )
        target = (destination / member.name).resolve()
        if not target.is_relative_to(root):
            raise VerificationError(f"archive entry escapes destination: {member.name}")
        tar.extract(member, destination, **extract_kwargs)


def _verify_self_copy(archive: Path, extracted: Path) -> None:
    copy = extracted / archive.name
    if not copy.is_file():
        raise VerificationError(f"self copy missing from archive: {archive.name}")
    if _sha256_file(copy) != _sha256_file(archive):
        raise VerificationError(
            f"self copy does not match the archive: {archive.name}"
        )


def _verify_payload(
    source: Path,
    extracted: Path,
    archive_name: str,
    excludes: Sequence[str],
) -> None:
    payload = scan_payload(source, excludes)
    try:
        expected = {entry.path: entry for entry in payload.files}
        expected_dirs = {entry.path for entry in payload.dirs}
        actual = _extracted_files(extracted, archive_name)
        actual_dirs = _extracted_dirs(extracted)

        missing = sorted(set(expected) - set(actual))
        if missing:
            raise VerificationError(
                f"payload entries missing from archive: {_preview(missing)}"
            )
        unexpected = sorted(set(actual) - set(expected))
        if unexpected:
            raise VerificationError(
                f"unexpected entries in archive: {_preview(unexpected)}"
            )

        missing_dirs = sorted(expected_dirs - actual_dirs)
        if missing_dirs:
            raise VerificationError(
                f"payload directories missing from archive: {_preview(missing_dirs)}"
            )
        unexpected_dirs = sorted(actual_dirs - expected_dirs)
        if unexpected_dirs:
            raise VerificationError(
                f"unexpected directories in archive: {_preview(unexpected_dirs)}"
            )

        for path, entry in expected.items():
            if _sha256_bytes(entry.data or b"") != _sha256_file(actual[path]):
                raise VerificationError(f"payload entry does not match: {path}")
    finally:
        payload.close()


def _extracted_dirs(root: Path) -> set[str]:
    directories: set[str] = set()
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        rel_parts = current.relative_to(root).parts
        if not rel_parts:
            dirnames[:] = [name for name in dirnames if name != SEED_DIR]
            continue
        directories.add("/".join(rel_parts))
    return directories


def _extracted_files(root: Path, archive_name: str) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_parts = current.relative_to(root).parts
        if not rel_parts:
            dirnames[:] = [name for name in dirnames if name != SEED_DIR]
        for name in filenames:
            if not rel_parts and name == archive_name:
                continue
            rel = "/".join((*rel_parts, name))
            files[rel] = current / name
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VerificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _preview(paths: list[str], limit: int = 5) -> str:
    if len(paths) <= limit:
        return ", ".join(paths)
    return f"{', '.join(paths[:limit])} (+{len(paths) - limit} more)"


def _extraction_failure(archive: Path, completed: subprocess.CompletedProcess[str]) -> str:
    output = (completed.stderr or completed.stdout or "").strip()
    if len(output) > 500:
        output = output[-500:]
    message = f"7-Zip failed to extract {archive.name} (exit {completed.returncode})"
    return f"{message}: {output}" if output else message