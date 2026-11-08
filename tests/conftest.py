"""Shared test doubles.

Everything the engine touches is faked here, so the test suite runs on any
platform with no Windows, no Roblox and no real waiting.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tds_macro.input.coords import Rect, VirtualScreen  # noqa: E402
from tds_macro.strategy.engine import GameState, RunStatus, StrategyRunner  # noqa: E402


class FakeClock:
    """Monotonic millisecond clock under the test's control."""

    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms


@dataclass
class FakeFocus:
    foreground: bool = True

    def is_foreground(self) -> bool:
        return self.foreground


@dataclass
class FakeState:
    """Scriptable game state, with an optional per-snapshot hook."""

    wave: int | None = 1
    cash: int | None = 1000
    confidence: float = 1.0
    end_screen: bool = False
    clock: FakeClock | None = None
    snapshots: int = 0
    on_snapshot: object = None

    def snapshot(self) -> GameState:
        self.snapshots += 1
        if callable(self.on_snapshot):
            self.on_snapshot(self)
        return GameState(
            wave=self.wave,
            cash=self.cash,
            confidence=self.confidence,
            end_screen=self.end_screen,
            timestamp_ms=self.clock.value if self.clock else 0,
        )


@dataclass
class LogCapture:
    entries: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, level: str, message: str) -> None:
        self.entries.append((level, message))

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lvl, m in self.entries if level is None or lvl == level]

    def text(self) -> str:
        return "\n".join(f"{lvl}: {m}" for lvl, m in self.entries)


def pump(runner: StrategyRunner, clock: FakeClock, limit_ms: int = 10 * 60 * 1000,
         tick_ms: int = 5) -> int:
    """Drive a runner to completion, jumping the clock to each wake time.

    Returns simulated elapsed milliseconds.  Raises if the run does not settle,
    which catches a step generator that never yields progress.
    """
    start = clock.value
    guard = 0
    while runner.status.is_active:
        runner.tick()
        guard += 1
        if guard > 200_000:
            raise AssertionError(f"runner did not settle; status={runner.status}")
        if clock.value - start > limit_ms:
            raise AssertionError(f"runner exceeded {limit_ms} ms; status={runner.status}")
        target = runner.next_wake_at
        clock.value = max(clock.value + tick_ms, target) if target else clock.value + tick_ms
    return clock.value - start


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def focus() -> FakeFocus:
    return FakeFocus()


@pytest.fixture
def logs() -> LogCapture:
    return LogCapture()


@pytest.fixture
def client_rect() -> Rect:
    return Rect(100, 50, 1380, 770)


@pytest.fixture
def virtual_screen() -> VirtualScreen:
    return VirtualScreen(0, 0, 1920, 1080)


__all__ = [
    "FakeClock",
    "FakeFocus",
    "FakeState",
    "LogCapture",
    "RunStatus",
    "pump",
]
