"""Logging and metrics.

Two constraints shaped this module:

**Logs are for humans reading a file, metrics are for programs.** Mixing them
(e.g. emitting Prometheus text into the log) produces logs nobody can scan and a
metrics endpoint with no labels. They are kept separate.

**Nothing here changes behaviour.** Logging must never raise into a request: a
full disk or an encoding error in a log call must not turn a successful embed
into a 500. Every entry point swallows its own failures.

Logs are line-oriented ``key=value`` rather than JSON. Reason: this project's
messages are Chinese and its operators read them in a terminal; JSON forces every
line through a parser to answer "what did item 300 fail on". The format is stable
and machine-parseable (``key=value`` pairs, values with spaces/quotes quoted).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

LOGGER_NAME = "markovcryptor"

#: Values at or above this are logged by default; ``--log-level`` overrides.
DEFAULT_LEVEL = "INFO"


class _Formatter(logging.Formatter):
    """``time level event k=v k=v`` on one line."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        base = super().format(record)
        if fields:
            base += " " + _render(fields)
        return base


def _render(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        text = str(value)
        if text == "" or any(c in text for c in ' \t"=\n'):
            text = '"' + text.replace('"', '\\"').replace("\n", " ") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def configure(level: str | int | None = None, *, stream=None,
              log_file: str | Path | None = None) -> logging.Logger:
    """Attach the project handler.  Idempotent: calling twice adds nothing."""
    global _configured
    _configured = True
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        # Must close: on Windows an open FileHandler keeps the file locked and
        # the caller cannot delete or rotate it.
        try:
            handler.close()
        except Exception:                          # noqa: BLE001
            pass

    handlers: list[logging.Handler] = []
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8 regardless of platform: the messages are Chinese and the ANSI
        # code page would raise on some of them.
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(stream or sys.stderr))

    for handler in handlers:
        handler.setFormatter(_Formatter())
        logger.addHandler(handler)
    return logger


def _coerce_level(level: str | int | None) -> int:
    if level is None:
        level = os.environ.get("MKX1_LOG_LEVEL", DEFAULT_LEVEL)
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


#: Set once an entry point calls :func:. Until then nothing is
#: emitted: a library that writes to stderr on import makes every test run and
#: every embedder's console noisy with lines nobody asked for. The CLI and the
#: web server configure explicitly; a library user who wants logs calls it too.
_configured = False


def event(suffix: str, name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event.  Never raises."""
    if not _configured:
        return
    try:
        get_logger(suffix).log(level, name, extra={"fields": fields})
    except Exception:                              # noqa: BLE001
        # A broken log sink must not fail the operation being logged.
        pass


# --------------------------------------------------------------------- metrics

@dataclass
class Metrics:
    """Counters and latency totals, in-process only.

    Deliberately not a Prometheus client dependency: this is a single-machine
    local service and the endpoint needs to answer "is anything failing, and how
    slow is it".  Counters are monotonic since process start; latency is kept as
    a count and a sum so the endpoint can report a mean without keeping samples.
    """

    started_at: float = field(default_factory=time.time)
    counters: Counter = field(default_factory=Counter)
    totals: dict[str, Counter] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self.counters[key] += value

    def observe(self, name: str, milliseconds: float, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            bucket = self.totals.setdefault(key, Counter())
            bucket["count"] += 1
            bucket["sum_ms"] += milliseconds

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self.counters)
            latencies = {
                key: {"count": bucket["count"],
                      "mean_ms": round(bucket["sum_ms"] / bucket["count"], 2)
                      if bucket["count"] else 0.0}
                for key, bucket in self.totals.items()
            }
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": counters,
            "latency": latencies,
        }

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.totals.clear()
            self.started_at = time.time()


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    suffix = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{suffix}}}"


#: The process-wide instance.  Created eagerly so counters survive across
#: requests without any wiring.
METRICS = Metrics()
