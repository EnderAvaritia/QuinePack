"""Shared data model for QuinePack payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PayloadEntry:
    """One archive entry, ordered as it will appear in the archive."""

    path: str
    is_dir: bool
    data: bytes | None
    mtime_ns: int
    mode: int = 0o644
    attributes: int = 0


@dataclass(frozen=True, slots=True)
class Payload:
    """A scanned local file or directory tree."""

    source: Path
    entries: tuple[PayloadEntry, ...]
    total_bytes: int
    is_single_file: bool

    @property
    def files(self) -> tuple[PayloadEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.is_dir)

    @property
    def dirs(self) -> tuple[PayloadEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_dir)

    @property
    def content(self) -> bytes:
        return b"".join(entry.data or b"" for entry in self.files)