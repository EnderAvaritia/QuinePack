"""Process-parallel task runner for the CPU-bound encoders.

``multiprocessing`` re-imports the caller's ``__main__`` under the ``spawn``
start method, which re-runs unguarded user scripts.  These workers are started
with ``-c`` instead and only ever import QuinePack, so a pool is safe to open
from a script, a REPL or a test runner.

Every task must be a pure function of its arguments, and results are returned
in submission order, so parallel work produces exactly the same bytes as the
in-process loop.  Small batches, and environments where workers cannot start,
fall back to that loop.
"""

from __future__ import annotations

import os
import pickle
import struct
import subprocess
import sys
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")

# Starting a worker costs about 60 ms, so callers gate on payload size.
MAX_WORKERS = 16
DEFAULT_CHUNKSIZE = 16

# Frames are length-prefixed pickles.  Keeping one chunk per message (rather
# than one bucket per worker) keeps every write well below the pipe buffer.
_LENGTH = struct.Struct("<I")

_BOOTSTRAP = r"""
import pickle, struct, sys, traceback
from importlib import import_module

module_name, _, attribute = sys.argv[1].partition(":")
function = getattr(import_module(module_name), attribute)
source, sink = sys.stdin.buffer, sys.stdout.buffer
while True:
    header = source.read(4)
    if len(header) < 4:
        break
    size = struct.unpack("<I", header)[0]
    tasks = pickle.loads(source.read(size))
    results = []
    for task in tasks:
        try:
            results.append((True, function(task)))
        except BaseException:
            results.append((False, traceback.format_exc()))
    payload = pickle.dumps(results, protocol=pickle.HIGHEST_PROTOCOL)
    sink.write(struct.pack("<I", len(payload)) + payload)
    sink.flush()
"""


def worker_count() -> int:
    """Number of workers to start, honouring the environment overrides.

    ``QUINEPACK_NO_MP`` disables worker processes, ``QUINEPACK_WORKERS`` caps
    the count (the multi-format build splits the cores between its children).
    """
    if os.environ.get("QUINEPACK_NO_MP"):
        return 1
    override = os.environ.get("QUINEPACK_WORKERS")
    if override:
        try:
            return max(1, min(int(override), MAX_WORKERS))
        except ValueError:
            pass
    try:
        count = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        count = os.cpu_count() or 1
    return max(1, min(count, MAX_WORKERS))


def _qualified_name(function: Callable) -> str:
    module = getattr(function, "__module__", None)
    name = getattr(function, "__qualname__", None)
    if not module or not name or "." in name or "<" in name:
        raise ValueError(f"{function!r} is not a module-level function")
    return f"{module}:{name}"


class TaskPool(Generic[T, R]):
    """A small ordered pool of ``python -c`` workers running one function."""

    def __init__(
        self,
        function: Callable[[T], R],
        workers: int | None = None,
    ) -> None:
        self._function = function
        self._target = _qualified_name(function)
        self._processes: list[subprocess.Popen[bytes]] = []
        self._chunksize = DEFAULT_CHUNKSIZE
        limit = worker_count() if workers is None else workers
        if limit < 2:
            return
        environment = os.environ.copy()
        path = [entry for entry in sys.path if entry]
        inherited = environment.get("PYTHONPATH")
        if inherited:
            path.append(inherited)
        environment["PYTHONPATH"] = os.pathsep.join(path)
        try:
            for _ in range(limit):
                self._processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", _BOOTSTRAP, self._target],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        env=environment,
                    )
                )
        except OSError:
            self.close()

    @property
    def parallel(self) -> bool:
        return bool(self._processes)

    def map(self, tasks: Sequence[T], chunksize: int | None = None) -> list[R]:
        """Apply the pool function to every task, preserving order."""
        if not tasks:
            return []
        if not self._processes:
            return [self._function(task) for task in tasks]
        size = self._chunksize if chunksize is None else max(1, chunksize)
        chunks = [
            list(tasks[offset : offset + size])
            for offset in range(0, len(tasks), size)
        ]
        results: list[list[R] | None] = [None] * len(chunks)
        pending = iter(range(len(chunks)))
        active: list[tuple[subprocess.Popen[bytes], int]] = []
        for process in self._processes:
            index = next(pending, None)
            if index is None:
                break
            self._send(process, chunks[index])
            active.append((process, index))
        while active:
            process, index = active.pop(0)
            results[index] = self._receive(process)
            following = next(pending, None)
            if following is not None:
                self._send(process, chunks[following])
                active.append((process, following))
        return [item for chunk in results if chunk is not None for item in chunk]

    def _send(self, process: subprocess.Popen[bytes], tasks: list[T]) -> None:
        assert process.stdin is not None
        payload = pickle.dumps(tasks, protocol=pickle.HIGHEST_PROTOCOL)
        process.stdin.write(_LENGTH.pack(len(payload)) + payload)
        process.stdin.flush()

    def _receive(self, process: subprocess.Popen[bytes]) -> list[R]:
        assert process.stdout is not None
        header = process.stdout.read(_LENGTH.size)
        if len(header) < _LENGTH.size:
            raise RuntimeError("QuinePack worker exited before returning a result")
        (size,) = _LENGTH.unpack(header)
        payload = process.stdout.read(size)
        if len(payload) < size:
            raise RuntimeError("QuinePack worker truncated its result")
        results = pickle.loads(payload)
        values: list[R] = []
        for ok, value in results:
            if not ok:
                raise RuntimeError(f"QuinePack worker failed:\n{value}")
            values.append(value)
        return values

    def close(self) -> None:
        for process in self._processes:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
        for process in self._processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        self._processes.clear()

    def __enter__(self) -> "TaskPool[T, R]":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_pool(function: Callable[[T], R], *, enabled: bool) -> TaskPool[T, R]:
    """Return a pool, degrading to in-process work if workers cannot start."""
    if not enabled:
        return TaskPool(function, workers=1)
    try:
        return TaskPool(function)
    except (OSError, ValueError, subprocess.SubprocessError):
        return TaskPool(function, workers=1)