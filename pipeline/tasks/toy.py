"""Toy tasks. They prove the engine without any real data.

Workflows:
  toy          fan out n hash tasks, then one join (the chaos workflow)
  toy_sleep    one task that sleeps (kill and zombie tests)
  toy_failures one fatal task, one flaky task, a strict child and a tolerant join
"""
from __future__ import annotations

import hashlib
import time

from capitol_sdk import Fatal, Record, Result, TaskSpec, task, workflow


@workflow("toy")
def toy(params: dict) -> list[TaskSpec]:
    return [TaskSpec("fan_out", "root", {"n": int(params.get("n", 2000)), "max_sleep_ms": int(params.get("max_sleep_ms", 20))})]


@task("fan_out", timeout_s=60)
def fan_out(ctx, payload):
    n = payload["n"]
    leaves = [TaskSpec("hash_leaf", f"{i:06d}", {"i": i, "max_sleep_ms": payload["max_sleep_ms"]}) for i in range(n)]
    join = TaskSpec("hash_join", "all", {"n": n}, depends_on=[leaf.ref for leaf in leaves])
    return Result(output={"children": n}, sub_dag=[*leaves, join])


@task("hash_leaf", timeout_s=30)
def hash_leaf(ctx, payload):
    # The sleep comes from ctx.rng, so it is the same in every run.
    time.sleep(ctx.rng.uniform(0, payload["max_sleep_ms"]) / 1000)
    digest = hashlib.sha256(f"{ctx.seed}:{payload['i']}".encode()).hexdigest()
    return Result(records=[Record("leaf_hash", ctx.key, {"i": payload["i"], "sha256": digest})])


@task("hash_join", timeout_s=60)
def hash_join(ctx, payload):
    combined = hashlib.sha256()
    count = 0
    for key, data in ctx.records("leaf_hash"):  # always sorted by key
        combined.update(f"{key}:{data['sha256']}\n".encode())
        count += 1
    if count != payload["n"]:
        raise Fatal(f"expected {payload['n']} leaf hashes, found {count}")
    return Result(records=[Record("combined_hash", "all", {"count": count, "sha256": combined.hexdigest()})])


@workflow("toy_sleep")
def toy_sleep(params: dict) -> list[TaskSpec]:
    return [TaskSpec("sleeper", "1", {"seconds": float(params.get("seconds", 3))})]


@task("sleeper", timeout_s=120)
def sleeper(ctx, payload):
    time.sleep(payload["seconds"])
    return Result(records=[Record("slept", ctx.key, {"seconds": payload["seconds"]})])


@workflow("toy_failures")
def toy_failures(params: dict) -> list[TaskSpec]:
    return [
        TaskSpec("always_fatal", "1"),
        TaskSpec("flaky", "1", {"fail_first": int(params.get("fail_first", 3))}),
        TaskSpec("after_fatal", "1", depends_on=["always_fatal/1"]),
        TaskSpec("tolerant_join", "1", depends_on=["always_fatal/1", "flaky/1"], tolerate_failures=True),
    ]


@task("always_fatal")
def always_fatal(ctx, payload):
    raise Fatal("this task can never work")


@task("flaky")
def flaky(ctx, payload):
    # Test-only: real tasks must not look at ctx.attempt (design rule 7).
    if ctx.attempt <= payload["fail_first"]:
        raise RuntimeError(f"flaky failure on attempt {ctx.attempt}")
    return Result(records=[Record("flaky_done", ctx.key, {"ok": True})])


@task("after_fatal")
def after_fatal(ctx, payload):
    return Result(records=[Record("should_not_exist", ctx.key, {})])


@task("tolerant_join")
def tolerant_join(ctx, payload):
    return Result(records=[Record("join_done", ctx.key, {"flaky": len(ctx.records("flaky_done"))})])
