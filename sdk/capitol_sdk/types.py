"""The small set of types a task author needs."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

FLOAT_DIGITS = 10


class Fatal(Exception):
    """Raise when retrying cannot help. The task goes to the dead-letter queue."""


class RetryAfter(Exception):
    """Raise to retry after a delay the remote server asked for."""

    def __init__(self, seconds: float, message: str = ""):
        super().__init__(message or f"retry after {seconds}s")
        self.seconds = max(1, math.ceil(seconds))


@dataclass(frozen=True)
class Record:
    """One row of domain output. (kind, key) is unique inside a run."""

    kind: str
    key: str
    data: Any


@dataclass(frozen=True)
class TaskSpec:
    """A task to create. depends_on holds "type/key" of tasks in the same list."""

    type: str
    key: str
    payload: Any = None
    depends_on: tuple[str, ...] | list[str] = ()
    rate_key: str = ""
    tolerate_failures: bool = False
    max_attempts: int = 0  # 0 = server default

    @property
    def ref(self) -> str:
        return f"{self.type}/{self.key}"


@dataclass
class Result:
    """What a task returns. All of it is committed in one transaction."""

    output: Any = None
    records: list[Record] = field(default_factory=list)
    sub_dag: list[TaskSpec] = field(default_factory=list)


def task_id(run_id: str, task_type: str, key: str) -> str:
    """Same value as engine.TaskID in the Go coordinator (design rule 6)."""
    return hashlib.sha256(f"{run_id}\n{task_type}\n{key}".encode()).hexdigest()


def _normalise(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and infinity are not allowed in task output")
        rounded = round(value, FLOAT_DIGITS)
        return 0.0 if rounded == 0 else rounded  # no "-0.0"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"JSON object keys must be strings, got {type(k).__name__}")
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    raise TypeError(f"{type(value).__name__} cannot be stored as JSON")


def canonical_json(value: Any) -> bytes:
    """JSON with sorted keys and floats rounded to 10 places.

    Two runs that compute the same result must produce the same bytes, so
    outputs can be compared by hash (rules 7 and 8 in docs/design.md).
    """
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
