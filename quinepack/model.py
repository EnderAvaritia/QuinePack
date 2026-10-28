"""Shared data model for QuinePack payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .buffers import Buffer, ContentStore


@dataclass(frozen=True, slots=True)
class PayloadEntry:
    """One archive entry, ordered as it will appear in the archive."""

    path: str
    is_dir: bool
    data: Buffer | None
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
    _store: ContentStore | None = field(
        default=None, compare=False, repr=False, kw_only=True
    )
    _released: bool = field(
        default=False, compare=False, repr=False, kw_only=True
    )

    @property
    def files(self) -> tuple[PayloadEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.is_dir)

    @property
    def dirs(self) -> tuple[PayloadEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_dir)

    @property
    def content(self) -> bytes:
        return b"".join(entry.data or b"" for entry in self.files)

    @property
    def content_view(self) -> Buffer:
        """Contiguous payload bytes without joining when a store is present."""
        if self._store is not None:
            view = self._store.view()
            return view[: self.total_bytes]
        files = self.files
        if len(files) == 1:
            return files[0].data or b""
        return b"".join(entry.data or b"" for entry in files)

    def release_content(self) -> None:
        """Release mapped payload views and their temporary files.

        The caller must not access file contents afterwards.  Metadata such as
        paths, sizes and mtimes remains available.
        """
        if self._released:
            return
        for entry in self.entries:
            data = entry.data
            if isinstance(data, memoryview):
                try:
                    data.release()
                except (BufferError, ValueError):
                    pass
        if self._store is not None:
            self._store.close()
        object.__setattr__(self, "_released", True)

    def close(self) -> None:
        self.release_content()
