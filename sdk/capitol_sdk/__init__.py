"""capitol_sdk: write tasks for the Capitol Trades Engine.

    from capitol_sdk import task, Result, Record, TaskSpec, Fatal, RetryAfter

    @task("parse_filing", timeout_s=120)
    def parse_filing(ctx, payload):
        ...
        return Result(records=[Record("trade", key, data)])
"""
from .registry import task, workflow
from .types import Fatal, Record, Result, RetryAfter, TaskSpec, canonical_json, task_id

__all__ = [
    "Fatal",
    "Record",
    "Result",
    "RetryAfter",
    "TaskSpec",
    "canonical_json",
    "run_local",
    "task",
    "task_id",
    "workflow",
]


def run_local(*args, **kwargs):
    """See capitol_sdk.local.run_local."""
    from .local import run_local as _run_local

    return _run_local(*args, **kwargs)
