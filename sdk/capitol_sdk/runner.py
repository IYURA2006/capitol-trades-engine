"""Run one task function and turn what it returns into plain data.

Used in two places: inside the worker's child process, and by run_local.
Keeping it in one function means both paths treat a task the same way.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any

from .context import Context
from .registry import TASKS
from .types import Fatal, Record, Result, RetryAfter, TaskSpec, canonical_json


@dataclass
class Outcome:
    """Picklable result of one task execution."""

    status: str  # "ok", "retry" or "fatal"
    output: bytes = b""
    records: list[tuple[str, str, bytes]] = field(default_factory=list)
    sub_dag: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    retry_after_s: int = 0


def spec_to_dict(spec: TaskSpec) -> dict[str, Any]:
    return {
        "type": spec.type,
        "key": spec.key,
        "payload": canonical_json({} if spec.payload is None else spec.payload),
        "depends_on": list(spec.depends_on),
        "rate_key": spec.rate_key,
        "tolerate_failures": spec.tolerate_failures,
        "max_attempts": spec.max_attempts,
    }


def execute(ctx: Context, payload: Any) -> Outcome:
    """Call the task function. Never raises: every error becomes an Outcome."""
    try:
        definition = TASKS.get(ctx.task_type)
        if definition is None:
            return Outcome("fatal", error=f"no task registered for type {ctx.task_type!r}")
        result = definition.fn(ctx, payload)
        if result is None:
            result = Result()
        if not isinstance(result, Result):
            return Outcome("fatal", error=f"task returned {type(result).__name__}, expected Result or None")
        seen: set[tuple[str, str]] = set()
        records = []
        for r in result.records:
            if not isinstance(r, Record):
                return Outcome("fatal", error="Result.records must hold Record objects")
            if (r.kind, r.key) in seen:
                return Outcome("fatal", error=f"record {r.kind}/{r.key} returned twice")
            seen.add((r.kind, r.key))
            records.append((r.kind, r.key, canonical_json(r.data)))
        return Outcome(
            "ok",
            output=b"" if result.output is None else canonical_json(result.output),
            records=records,
            sub_dag=[spec_to_dict(s) for s in result.sub_dag],
        )
    except Fatal as err:
        return Outcome("fatal", error=f"Fatal: {err}")
    except RetryAfter as err:
        return Outcome("retry", error=str(err), retry_after_s=err.seconds)
    except Exception as err:  # noqa: BLE001 - any other error is worth another try
        tail = traceback.format_exc(limit=4)
        return Outcome("retry", error=f"{type(err).__name__}: {err}\n{tail}"[:4000])
