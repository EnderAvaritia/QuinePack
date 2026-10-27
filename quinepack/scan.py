"""Payload scanning (product spec section 7).

Stage 1 reads file contents into memory; the 1 GiB cap keeps that bounded.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import PayloadError
from .model import Payload, PayloadEntry
from .naming import SEED_DIR

MAX_PAYLOAD_BYTES = 1 << 30


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

    data = _read_file(source, f"file: {source}", size)
    entry = PayloadEntry(
        path=source.name,
        is_dir=False,
        data=data,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        attributes=getattr(info, "st_file_attributes", 0),
    )
    return Payload(
        source=source,
        entries=(entry,),
        total_bytes=len(data),
        is_single_file=True,
    )


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
        self.entries: list[PayloadEntry] = []
        self.total_bytes = 0

    def run(self) -> Payload:
        reserved = self.root / SEED_DIR
        if reserved.exists() or reserved.is_symlink():
            raise PayloadError(
                f"payload root contains the reserved directory {SEED_DIR!r}: {reserved}"
            )
        self._walk(self.root, (), frozenset({os.path.realpath(self.root)}))
        return Payload(
            source=self.root,
            entries=tuple(self.entries),
            total_bytes=self.total_bytes,
            is_single_file=False,
        )

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
            self.entries.append(
                PayloadEntry(
                    path=rel,
                    is_dir=True,
                    data=None,
                    mtime_ns=info.st_mtime_ns,
                    mode=stat.S_IMODE(info.st_mode),
                    attributes=getattr(info, "st_file_attributes", 0),
                )
            )
        for child, info, rel in files:
            self.entries.append(self._read_entry(child, info, rel))

        for child, _, rel in directories:
            real = os.path.realpath(child)
            if real in ancestors:
                raise PayloadError(f"symbolic link cycle detected at {rel}")
            self._walk(child, (*rel_parts, child.name), ancestors | {real})

    def _read_entry(self, child: Path, info: os.stat_result, rel: str) -> PayloadEntry:
        size = info.st_size
        self.total_bytes += size
        if self.total_bytes > MAX_PAYLOAD_BYTES:
            raise PayloadError(_too_large_message(self.total_bytes))

        data = _read_file(child, rel, size)
        try:
            after = child.stat()
        except OSError as exc:
            raise PayloadError(f"cannot stat {rel}: {exc}") from exc
        if after.st_size != size or after.st_mtime_ns != info.st_mtime_ns:
            raise PayloadError(f"file changed while reading: {rel}")

        return PayloadEntry(
            path=rel,
            is_dir=False,
            data=data,
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


def _read_file(path: Path, label: str, expected_size: int) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PayloadError(f"cannot read {label}: {exc}") from exc
    if len(data) != expected_size:
        raise PayloadError(f"file changed while reading: {label}")
    return data


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