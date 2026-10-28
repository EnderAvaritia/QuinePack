"""Tests for the subprocess task pool."""

from __future__ import annotations

import pytest

from quinepack.parallel import TaskPool, open_pool


def _double(value: int) -> int:
    return value * 2


def _explode(value: int) -> int:
    raise ValueError(f"boom: {value}")


def test_task_pool_preserves_order() -> None:
    tasks = list(range(200))
    with TaskPool(_double, workers=2) as pool:
        assert pool.parallel
        assert pool.map(tasks, chunksize=7) == [value * 2 for value in tasks]


def test_task_pool_handles_empty_batches() -> None:
    with TaskPool(_double, workers=2) as pool:
        assert pool.map([]) == []


def test_open_pool_falls_back_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUINEPACK_NO_MP", "1")
    with open_pool(_double, enabled=True) as pool:
        assert not pool.parallel
        assert pool.map([1, 2, 3]) == [2, 4, 6]


def test_task_pool_surfaces_worker_errors() -> None:
    with TaskPool(_explode, workers=2) as pool:
        with pytest.raises(RuntimeError, match="boom: 1"):
            pool.map([1])