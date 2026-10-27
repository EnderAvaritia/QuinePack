"""Artifact naming and layout safety rules (product spec section 5)."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import PayloadError
from .model import Payload

SEED_DIR = ".quinepack"
FORMAT_KEYS: tuple[str, ...] = ("7z", "7z-store", "txz")
COMPRESSED_FORMATS = frozenset({"7z"})

# Suffix order matters when detecting a format from a file name.
_FORMAT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("7z-store", ".store.7z"),
    ("7z", ".7z"),
    ("txz", ".tar.xz"),
)


def format_is_compressed(format_key: str) -> bool:
    """Return True when the format promises real compression."""

    return format_key in COMPRESSED_FORMATS


def artifact_name(input_path: Path, format_key: str) -> str:
    """Derive the artifact file name for an input path and format key."""

    suffix = dict(_FORMAT_SUFFIXES).get(format_key)
    if suffix is None:
        raise ValueError(f"unsupported format: {format_key}")
    path = Path(input_path)
    name = path.name or path.resolve().name
    if not name:
        raise ValueError(f"cannot derive an artifact name from {input_path!s}")
    return f"{name}{suffix}"


def seed_name(artifact: str) -> str:
    """Return the root-relative POSIX path of the seed entry."""

    return f"{SEED_DIR}/{artifact}.seed"


def format_from_artifact_name(name: str) -> str | None:
    """Detect a known format from an artifact file name, or return None."""

    filename = Path(name).name.lower()
    for format_key, suffix in _FORMAT_SUFFIXES:
        if filename.endswith(suffix):
            return format_key
    return None


def _same_name(left: str, right: str) -> bool:
    """Compare entry names the way the host filesystem would."""

    if os.name == "nt":
        return left.casefold() == right.casefold()
    return left == right


def ensure_artifact_not_in_payload(payload: Payload, artifact: str) -> None:
    """Reject a payload that already contains an entry named like the artifact."""

    for entry in payload.entries:
        if _same_name(entry.path, artifact):
            raise PayloadError(f"payload already contains an entry named {artifact!r}")


def ensure_output_not_inside_payload(payload: Payload, output: Path) -> None:
    """Reject outputs that would be written into the payload itself."""

    try:
        source = payload.source.resolve()
        target = Path(output).resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise PayloadError(f"cannot resolve output path {output}: {exc}") from exc

    if payload.is_single_file:
        if target == source:
            raise PayloadError(f"output path would overwrite the payload: {output}")
        return

    if target == source or target.is_relative_to(source):
        raise PayloadError(f"output path is inside the payload directory: {output}")