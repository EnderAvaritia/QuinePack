"""Stage reporting for writers (product spec section 8).

The CLI installs a sink around each build so writers can report their phases
without threading a callback through every builder constructor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

_sink: Callable[[str], None] | None = None


@contextmanager
def stage_sink(sink: Callable[[str], None]) -> Iterator[None]:
    """Route stage reports to ``sink`` for the duration of the block."""

    global _sink
    previous = _sink
    _sink = sink
    try:
        yield
    finally:
        _sink = previous


def report(stage: str) -> None:
    """Report a build stage when a sink is installed."""

    if _sink is not None:
        _sink(stage)