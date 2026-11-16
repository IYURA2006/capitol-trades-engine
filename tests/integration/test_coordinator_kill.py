"""Phase 4 acceptance test: kill a coordinator in the middle of a fan-out."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

import pipeline.tasks.toy  # noqa: F401  (registers the toy workflow for run_local)
from capitol_sdk import run_local

from .conftest import docker

pytestmark = pytest.mark.integration

# 10,000 on a laptop. A small CI machine does about 15 tasks a second, so CI
# sets a smaller fan-out; the insert is still one transaction of N + 1 rows.
N = int(os.environ.get("TEST_FANOUT_N", "10000"))
COORDINATORS = ["capitol-coordinator-1-1", "capitol-coordinator-2-1"]


def container_ip(name: str) -> str:
    return docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)


def test_coordinator_killed_while_inserting_the_children(stack, tmp_path):
    subprocess.run(["docker", "compose", "up", "-d", "--wait", "--scale", "worker=8", "--no-recreate"],
                   check=True, capture_output=True)
    by_ip = {container_ip(name): name for name in COORDINATORS}
    run_id = stack.submit("toy", n=N, max_sleep_ms=0)

    # Find the coordinator whose Postgres session is running the big INSERT
    # of the sub-DAG, and kill exactly that one, mid-transaction.
    victim, mid_insert = None, False
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            rows = stack.query(
                """SELECT host(client_addr) FROM pg_stat_activity
                   WHERE state = 'active' AND query LIKE '%%INSERT INTO task%%'
                     AND query NOT LIKE '%%pg_stat_activity%%'""")
            if rows and rows[0][0] in by_ip:
                victim, mid_insert = by_ip[rows[0][0]], True
                break
            if stack.one("SELECT count(*) FROM tasks WHERE run_id = %s", run_id) > 1:
                break  # the insert finished before we saw it
        if victim is None:
            victim = COORDINATORS[0]
        docker("kill", victim)
        tasks_after_kill = stack.one("SELECT count(*) FROM tasks WHERE run_id = %s", run_id)
        print(f"\nkilled {victim}; mid-insert: {mid_insert}; tasks visible right after the kill: {tasks_after_kill}")
        # All N + 1 children or none: never a part of the sub-DAG.
        assert tasks_after_kill in (1, N + 2)

        try:
            stack.wait_for("the run to succeed with one coordinator",
                           "SELECT state = 'SUCCEEDED' FROM runs WHERE id = %s", run_id, timeout=600)
        except AssertionError as error:
            states = dict(stack.query("SELECT state, count(*) FROM tasks WHERE run_id = %s GROUP BY state", run_id))
            raise AssertionError(f"{error}; tasks by state: {states}") from None
    finally:
        if victim:
            docker("start", victim)

    assert stack.one("SELECT count(*) FROM tasks WHERE run_id = %s", run_id) == N + 2
    assert stack.one("SELECT count(*) FROM tasks WHERE run_id = %s AND state <> 'SUCCEEDED'", run_id) == 0
    assert stack.one(
        """SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id
           WHERE t.run_id = %s AND t.type = 'hash_join' AND e.event = 'SUCCEEDED'""", run_id) == 1
    assert stack.one(
        """SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id
           WHERE t.run_id = %s AND t.type = 'hash_leaf' AND e.event = 'SUCCEEDED'
             AND e.seq > (SELECT min(j.seq) FROM task_events j JOIN tasks jt ON jt.id = j.task_id
                          WHERE jt.run_id = %s AND jt.type = 'hash_join' AND j.event = 'LEASED')""",
        run_id, run_id) == 0, "a leaf finished after the join was leased"

    # The result equals a single-process run of the same workflow.
    reference = run_local("toy", seed=0, params={"n": N, "max_sleep_ms": 0}, blob_dir=tmp_path)
    combined = stack.one("SELECT data FROM records WHERE run_id = %s AND kind = 'combined_hash'", run_id)
    assert combined == reference.records["combined_hash\nall"]
    assert stack.one("SELECT count(*) FROM records WHERE run_id = %s", run_id) == N + 1
