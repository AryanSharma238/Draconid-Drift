import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("timing")


class Timer:
    """Collects stage durations. header() -> Server-Timing header value."""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = (time.perf_counter() - t0) * 1000
            self.stages[name] = self.stages.get(name, 0.0) + dt
            log.info("stage=%s ms=%.1f", name, dt)

    def header(self) -> str:
        return ", ".join(f"{k};dur={v:.0f}" for k, v in self.stages.items())
