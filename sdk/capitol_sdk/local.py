"""run_local: the same task functions, one process, no coordinator.

It follows the engine's rules for dependencies, dead letters and cancels,
but has no leases, no retries in time and no concurrency. Its result is the
reference that every chaos run must match.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .context import BlobStore, Context, MemoryRecords, loads
from .registry import first_tasks
from .runner import execute, spec_to_dict

DEFAULT_MAX_ATTEMPTS = 5
TERMINAL_BAD = ("DEAD_LETTER", "CANCELLED")


@dataclass
class _Task:
    type: str
    key: str
    payload: bytes
    parents: list[str]
    tolerate_failures: bool
    max_attempts: int
    state: str = "PENDING"

    @property
    def ref(self) -> str:
        return f"{self.type}/{self.key}"


@dataclass
class LocalResult:
    tasks: dict[str, str] = field(default_factory=dict)  # "type/key" -> final state
    records: dict[str, Any] = field(default_factory=dict)  # "kind\nkey" -> data
    run_state: str = "SUCCEEDED"

    def to_json(self) -> str:
        records = [{"kind": k.split("\n", 1)[0], "key": k.split("\n", 1)[1], "data": v}
                   for k, v in sorted(self.records.items())]
        return json.dumps({"run_state": self.run_state, "tasks": self.tasks, "records": records},
                          sort_keys=True, ensure_ascii=False)


def run_local(workflow: str, snapshot_id: str = "", seed: int = 0, params: dict | None = None,
              *, blob_dir: str = "blobs", http: Any = None, run_id: str = "local") -> LocalResult:
    tasks: dict[str, _Task] = {}
    children: dict[str, list[str]] = {}
    store = MemoryRecords()
    blobs = BlobStore(blob_dir)

    def add(specs: list[dict]) -> None:
        refs = {f"{s['type']}/{s['key']}" for s in specs}
        if len(refs) != len(specs):
            raise ValueError("duplicate task in one DAG")
        for s in specs:
            ref = f"{s['type']}/{s['key']}"
            unknown = [d for d in s["depends_on"] if d not in refs]
            if unknown:
                raise ValueError(f"task {ref} depends on unknown task {unknown[0]}")
            if ref in tasks:
                continue  # same rule as ON CONFLICT DO NOTHING
            parents = sorted(set(s["depends_on"]))
            tasks[ref] = _Task(s["type"], s["key"], s["payload"], parents, s["tolerate_failures"],
                               s["max_attempts"] or DEFAULT_MAX_ATTEMPTS)
            for p in parents:
                children.setdefault(p, []).append(ref)

    def settle() -> None:
        """Cancel strict tasks with a failed parent, until nothing changes."""
        changed = True
        while changed:
            changed = False
            for t in tasks.values():
                if t.state == "PENDING" and not t.tolerate_failures and any(
                        tasks[p].state in TERMINAL_BAD for p in t.parents):
                    t.state = "CANCELLED"
                    changed = True

    def ready() -> list[_Task]:
        out = []
        for t in tasks.values():
            if t.state != "PENDING":
                continue
            states = [tasks[p].state for p in t.parents]
            if all(s == "SUCCEEDED" or (t.tolerate_failures and s in TERMINAL_BAD) for s in states):
                out.append(t)
        return sorted(out, key=lambda t: (t.type, t.key))

    add([spec_to_dict(s) for s in first_tasks(workflow, params)])
    while True:
        settle()
        batch = ready()
        if not batch:
            break
        for t in batch:
            for attempt in range(1, t.max_attempts + 1):
                ctx = Context(run_id=run_id, task_type=t.type, key=t.key, attempt=attempt, seed=seed,
                              snapshot_id=snapshot_id, blobs=blobs, records=store, http=http)
                outcome = execute(ctx, loads(t.payload))
                if outcome.status != "retry":
                    break
            if outcome.status == "ok":
                for kind, key, data in outcome.records:
                    if (kind, key) in store.data:
                        raise ValueError(f"record {kind}/{key} written twice (by {t.ref})")
                    store.data[(kind, key)] = json.loads(data)
                add(outcome.sub_dag)
                t.state = "SUCCEEDED"
            else:
                t.state = "DEAD_LETTER"

    stuck = [t.ref for t in tasks.values() if t.state == "PENDING"]
    if stuck:
        raise RuntimeError(f"{len(stuck)} tasks can never run (cycle?), e.g. {stuck[0]}")

    # Same rule as the engine: a run fails if a task was cancelled, or a task
    # dead-lettered and no child was there to tolerate it.
    failed = any(t.state == "CANCELLED" or (t.state == "DEAD_LETTER" and not children.get(t.ref))
                 for t in tasks.values())
    return LocalResult(
        tasks={ref: t.state for ref, t in sorted(tasks.items())},
        records={f"{kind}\n{key}": data for (kind, key), data in store.data.items()},
        run_state="FAILED" if failed else "SUCCEEDED",
    )
