"""Progress events emitted by the core pipeline — the only core→GUI bridge.

The GUI worker thread converts these into Qt signals, so core code never
imports Qt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ProgressEvent:
    message: str = ""
    done: int = 0
    total: int = 0


@dataclass
class BatchStarted(ProgressEvent):
    batch_index: int = 0
    batch_count: int = 0


@dataclass
class BatchFinished(ProgressEvent):
    batch_index: int = 0


@dataclass
class LineTranslated(ProgressEvent):
    position: int = 0
    reused: bool = False
    status: str = ""


@dataclass
class RetryScheduled(ProgressEvent):
    attempt: int = 0
    delay_seconds: float = 0.0


@dataclass
class WarningEvent(ProgressEvent):
    position: int | None = None


@dataclass
class RunFinished(ProgressEvent):
    ok: bool = True


Listener = Callable[[ProgressEvent], None]


class EventSource:
    """Tiny listener registry used by core components."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def add(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, event: ProgressEvent) -> None:
        for listener in list(self._listeners):
            listener(event)
