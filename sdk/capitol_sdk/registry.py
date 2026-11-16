"""Registries filled by the @task and @workflow decorators."""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .types import Result, TaskSpec


@dataclass(frozen=True)
class TaskDef:
    type: str
    fn: Callable[..., Result | None]
    timeout_s: float


TASKS: dict[str, TaskDef] = {}
WORKFLOWS: dict[str, Callable[[dict], list[TaskSpec]]] = {}


def task(task_type: str, timeout_s: float = 300):
    """Register a function as the handler for one task type.

    The function gets (ctx, payload) and returns a Result or None.
    It must be a pure function of its payload and the snapshot (rule 7).
    """

    def decorate(fn):
        if task_type in TASKS and TASKS[task_type].fn is not fn:
            raise ValueError(f"task type {task_type!r} is registered twice")
        TASKS[task_type] = TaskDef(task_type, fn, timeout_s)
        return fn

    return decorate


def workflow(name: str):
    """Register a function that returns the first tasks of a workflow."""

    def decorate(fn):
        WORKFLOWS[name] = fn
        return fn

    return decorate


def load_modules(modules: list[str]) -> None:
    for name in modules:
        if name:
            importlib.import_module(name)


def first_tasks(workflow_name: str, params: dict[str, Any] | None = None) -> list[TaskSpec]:
    if workflow_name not in WORKFLOWS:
        raise KeyError(f"unknown workflow {workflow_name!r}; known: {sorted(WORKFLOWS)}")
    return list(WORKFLOWS[workflow_name](params or {}))
