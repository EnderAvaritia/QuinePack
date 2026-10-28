"""Payload scanning (product spec section 7).

Small payloads stay in memory.  Once the scan exceeds ``SPOOL_THRESHOLD`` the
bytes are written to a temporary file and exposed through ``mmap``, so the
1 GiB cap no longer implies a 1 GiB Python-heap allocation.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .buffers import SPOOL_THRESHOLD, ContentStore
from .errors import PayloadError
from .model import Payload, PayloadEntry
from .naming import SEED_DIR

MAX_PAYLOAD_BYTES = 1 << 30
READ_CHUNK = 1 << 20


def scan_payload(
    input_path: Path,
    excludes: Sequence[str] = (),
    warn: Callable[[str], None] | None = None,
) -> Payload:
    """Scan a local file or directory tree into a payload."""

    source = Path(input_path)
    try:
        info = source.stat()
    except FileNotFoundError as exc:
        raise PayloadError(f"input does not exist: {source}") from exc
    except OSError as exc:
        raise PayloadError(f"cannot access input {source}: {exc}") from exc

    if stat.S_ISDIR(info.st_mode):
        return _DirectoryScanner(source, excludes, warn).run()
    if stat.S_ISREG(info.st_mode):
        return _scan_file(source, info)
    raise PayloadError(f"input is neither a regular file nor a directory: {source}")


def _scan_file(source: Path, info: os.stat_result) -> Payload:
    size = info.st_size
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadError(_too_large_message(size))

    store = ContentStore(threshold=SPOOL_THRESHOLD)
    try:
        _stream_file_into(source, f"file: {source}", size, store)
        try:
            after = source.stat()
        except OSError as exc:
            raise PayloadError(f"cannot stat {source}: {exc}") from exc
        if after.st_size != size or after.st_mtime_ns != info.st_mtime_ns:
            raise PayloadError(f"file changed while reading: {source}")
        view = store.view()
        entry = PayloadEntry(
            path=source.name,
            is_dir=False,
            data=view[0:size],
            mtime_ns=info.st_mtime_ns,
            mode=stat.S_IMODE(info.st_mode),
            attributes=getattr(info, "st_file_attributes", 0),
        )
        return Payload(
            source=source,
            entries=(entry,),
            total_bytes=size,
            is_single_file=True,
            _store=store,
        )
    except BaseException:
        store.close()
        raise


class _CaseFoldIndex:
    """Report paths that collide on a case-insensitive filesystem."""

    def __init__(self, warn: Callable[[str], None] | None) -> None:
        self.warn = warn
        self.seen: dict[str, str] = {}

    def note(self, path: str) -> None:
        key = path.casefold()
        previous = self.seen.get(key)
        if previous is None:
            self.seen[key] = path
        elif previous != path and self.warn is not None:
            self.warn(
                "case-insensitive path collision: "
                f"{previous!r} and {path!r}"
            )


@dataclass(slots=True)
class _PendingEntry:
    path: str
    is_dir: bool
    start: int
    size: int
    mtime_ns: int
    mode: int
    attributes: int


class _DirectoryScanner:
    """Depth-first walk that keeps directories before files on every level."""

    def __init__(
        self,
        root: Path,
        excludes: Sequence[str],
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self.patterns = _normalize_excludes(excludes)
        self.paths = _CaseFoldIndex(warn)
        self.store = ContentStore(threshold=SPOOL_THRESHOLD)
        self.pending: list[_PendingEntry] = []
        self.total_bytes = 0

    def run(self) -> Payload:
        try:
            reserved = self.root / SEED_DIR
            if reserved.exists() or reserved.is_symlink():
                raise PayloadError(
                    f"payload root contains the reserved directory {SEED_DIR!r}: "
                    f"{reserved}"
                )
            self._walk(self.root, (), frozenset({os.path.realpath(self.root)}))
            view = self.store.view()
            entries = tuple(
                PayloadEntry(
                    path=item.path,
                    is_dir=item.is_dir,
                    data=(
                        None
                        if item.is_dir
                        else view[item.start : item.start + item.size]
                    ),
                    mtime_ns=item.mtime_ns,
                    mode=item.mode,
                    attributes=item.attributes,
                )
                for item in self.pending
            )
            return Payload(
                source=self.root,
                entries=entries,
                total_bytes=self.total_bytes,
                is_single_file=False,
                _store=self.store,
            )
        except BaseException:
            self.store.close()
            raise

    def _walk(
        self,
        directory: Path,
        rel_parts: tuple[str, ...],
        ancestors: frozenset[str],
    ) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise PayloadError(f"cannot list directory {directory}: {exc}") from exc

        directories: list[tuple[Path, os.stat_result, str]] = []
        files: list[tuple[Path, os.stat_result, str]] = []
        for child in children:
            rel = "/".join((*rel_parts, child.name))
            if self._excluded(rel):
                continue
            try:
                info = child.stat()
            except OSError as exc:
                raise PayloadError(f"cannot stat {rel}: {exc}") from exc
            if stat.S_ISDIR(info.st_mode):
                self.paths.note(rel)
                directories.append((child, info, rel))
            elif stat.S_ISREG(info.st_mode):
                self.paths.note(rel)
                files.append((child, info, rel))
            else:
                raise PayloadError(f"unsupported file type: {rel}")

        for _, info, rel in directories:
            self.pending.append(
                _PendingEntry(
                    path=rel,
                    is_dir=True,
                    start=0,
                    size=0,
                    mtime_ns=info.st_mtime_ns,
                    mode=stat.S_IMODE(info.st_mode),
                    attributes=getattr(info, "st_file_attributes", 0),
                )
            )
        for child, info, rel in files:
            self.pending.append(self._read_entry(child, info, rel))

        for child, _, rel in directories:
            real = os.path.realpath(child)
            if real in ancestors:
                raise PayloadError(f"symbolic link cycle detected at {rel}")
            self._walk(child, (*rel_parts, child.name), ancestors | {real})

    def _read_entry(self, child: Path, info: os.stat_result, rel: str) -> _PendingEntry:
        size = info.st_size
        self.total_bytes += size
        if self.total_bytes > MAX_PAYLOAD_BYTES:
            raise PayloadError(_too_large_message(self.total_bytes))

        start = self.store.size
        _stream_file_into(child, rel, size, self.store)
        try:
            after = child.stat()
        except OSError as exc:
            raise PayloadError(f"cannot stat {rel}: {exc}") from exc
        if after.st_size != size or after.st_mtime_ns != info.st_mtime_ns:
            raise PayloadError(f"file changed while reading: {rel}")

        return _PendingEntry(
            path=rel,
            is_dir=False,
            start=start,
            size=size,
            mtime_ns=info.st_mtime_ns,
            mode=stat.S_IMODE(info.st_mode),
            attributes=getattr(info, "st_file_attributes", 0),
        )

    def _excluded(self, rel: str) -> bool:
        if os.name == "nt":
            rel = rel.casefold()
        return any(
            fnmatch.fnmatchcase(rel, pattern.casefold() if os.name == "nt" else pattern)
            for pattern in self.patterns
        )


def _stream_file_into(
    path: Path, label: str, expected_size: int, store: ContentStore
) -> None:
    """Copy a file into the content store without materialising it in RAM."""
    read = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                if read > expected_size:
                    raise PayloadError(f"file changed while reading: {label}")
                store.append(chunk)
    except OSError as exc:
        raise PayloadError(f"cannot read {label}: {exc}") from exc
    if read != expected_size:
        raise PayloadError(f"file changed while reading: {label}")


def _normalize_excludes(patterns: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for pattern in patterns:
        value = pattern.strip().replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        if value:
            normalized.append(value)
    return tuple(normalized)


def _too_large_message(size: int) -> str:
    return (
        f"payload is {size} bytes, which exceeds the 1 GiB limit "
        f"({MAX_PAYLOAD_BYTES} bytes)"
    )