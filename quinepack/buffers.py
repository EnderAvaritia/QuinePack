"""Byte buffers that move large payloads out of the Python heap.

Small values stay as ``bytes``/``bytearray`` so tests and ordinary payloads
keep the simple in-memory path.  Once a buffer crosses ``SPOOL_THRESHOLD`` it is
written to a temporary file and exposed through ``mmap``: builders can still
slice and CRC it like a normal buffer, but the operating system owns the pages
and can evict them under memory pressure.
"""

from __future__ import annotations

import mmap
import tempfile
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

Buffer = bytes | bytearray | memoryview

# 8 MiB keeps the existing in-memory tests fast while making the 1 GiB
# acceptance payload use file-backed pages.
SPOOL_THRESHOLD = 8 << 20


def _spool_base_dir() -> str:
    """Prefer the project directory over the system drive for temp files."""
    candidates = (Path.cwd(), Path(tempfile.gettempdir()))
    for candidate in candidates:
        try:
            probe = tempfile.TemporaryDirectory(prefix=".quinepack-probe-", dir=candidate)
        except OSError:
            continue
        probe.cleanup()
        return str(candidate)
    raise OSError("no writable directory available for QuinePack temporary files")


class SpoolHandle:
    """Owns the temporary file and mapping behind a spooled buffer."""

    def __init__(
        self,
        tempdir: tempfile.TemporaryDirectory[str],
        file: BinaryIO,
        mapping: mmap.mmap,
        view: memoryview,
    ) -> None:
        self._tempdir = tempdir
        self._file = file
        self._mapping = mapping
        self._view = view
        self._closed = False

    @property
    def view(self) -> memoryview:
        if self._closed:
            raise ValueError("spooled buffer is closed")
        return self._view

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._view.release()
        except (BufferError, ValueError):
            pass
        try:
            self._mapping.close()
        except BufferError:
            # External slices still reference the mapping; the file and
            # directory are cleaned up when those views are released.
            pass
        try:
            self._file.close()
        except OSError:
            pass
        try:
            self._tempdir.cleanup()
        except OSError:
            pass

    def __enter__(self) -> SpoolHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class SpoolWriter:
    """Append-only buffer that migrates to a mapped temp file when large."""

    def __init__(
        self,
        threshold: int = SPOOL_THRESHOLD,
        *,
        base_dir: str | Path | None = None,
    ) -> None:
        self._threshold = threshold
        self._base_dir = base_dir
        self._memory = bytearray()
        self._file: BinaryIO | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._size = 0
        self._finished = False

    @property
    def size(self) -> int:
        return self._size

    def write(self, data: Buffer) -> None:
        if self._finished:
            raise ValueError("cannot append to a finished spool")
        if not data:
            return
        if self._file is None and self._size + len(data) > self._threshold:
            self._migrate()
        if self._file is None:
            self._memory += data
        else:
            self._file.write(data)
        self._size += len(data)

    def _migrate(self) -> None:
        base_dir = self._base_dir if self._base_dir is not None else _spool_base_dir()
        self._tempdir = tempfile.TemporaryDirectory(
            prefix=".quinepack-spool-", dir=base_dir
        )
        path = Path(self._tempdir.name) / "buffer.bin"
        try:
            self._file = open(path, "w+b")
            self._file.write(self._memory)
        except OSError:
            self._cleanup_partial()
            raise
        self._memory = bytearray()

    def finish(self, *, writable: bool = False) -> tuple[Buffer, SpoolHandle | None]:
        """Return the accumulated buffer.

        Small buffers become ``bytes`` and own no handle.  Spooled buffers
        return a ``memoryview`` plus the handle that keeps the mapping alive.
        """
        if self._finished:
            raise ValueError("spool already finished")
        self._finished = True
        if self._file is None:
            return bytes(self._memory), None
        self._file.flush()
        assert self._file is not None
        access = mmap.ACCESS_WRITE if writable else mmap.ACCESS_READ
        mapping = mmap.mmap(self._file.fileno(), 0, access=access)
        view = memoryview(mapping)
        assert self._tempdir is not None
        handle = SpoolHandle(self._tempdir, self._file, mapping, view)
        self._tempdir = None
        self._file = None
        return view, handle

    def close(self) -> None:
        if self._tempdir is not None or self._file is not None:
            self._cleanup_partial()

    def _cleanup_partial(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._tempdir is not None:
            try:
                self._tempdir.cleanup()
            except OSError:
                pass
            self._tempdir = None

    def __enter__(self) -> SpoolWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class ContentStore:
    """Streaming store for payload bytes with offset-based views.

    Scanned entries keep an offset and size instead of a bytes object; the
    final ``view()`` is a single mapping that entries slice on demand.
    """

    def __init__(
        self,
        threshold: int = SPOOL_THRESHOLD,
        *,
        base_dir: str | Path | None = None,
    ) -> None:
        self._threshold = threshold
        self._base_dir = base_dir
        self._memory = bytearray()
        self._file: BinaryIO | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._mapping: mmap.mmap | None = None
        self._view: memoryview | None = None
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def append(self, data: Buffer) -> None:
        if self._view is not None:
            raise ValueError("cannot append after the store view was created")
        if not data:
            return
        if self._file is None and self._size + len(data) > self._threshold:
            self._migrate()
        if self._file is None:
            self._memory += data
        else:
            self._file.write(data)
        self._size += len(data)

    def _migrate(self) -> None:
        base_dir = self._base_dir if self._base_dir is not None else _spool_base_dir()
        self._tempdir = tempfile.TemporaryDirectory(
            prefix=".quinepack-spool-", dir=base_dir
        )
        path = Path(self._tempdir.name) / "payload.bin"
        try:
            self._file = open(path, "w+b")
            self._file.write(self._memory)
        except OSError:
            self.close()
            raise
        self._memory = bytearray()

    def view(self) -> memoryview:
        if self._view is not None:
            return self._view
        if self._file is None:
            self._view = memoryview(self._memory)
            return self._view
        self._file.flush()
        self._mapping = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        self._view = memoryview(self._mapping)
        return self._view

    def close(self) -> None:
        if self._view is not None:
            try:
                self._view.release()
            except (BufferError, ValueError):
                pass
            self._view = None
        if self._mapping is not None:
            try:
                self._mapping.close()
            except BufferError:
                pass
            self._mapping = None
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._tempdir is not None:
            try:
                self._tempdir.cleanup()
            except OSError:
                pass
            self._tempdir = None
        self._memory = bytearray()