"""Phase 3 acceptance tests: kill a worker, freeze a worker, failing tasks."""
from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest

from .conftest import LEASE_S, docker

pytestmark = pytest.mark.integration


def test_killed_worker_task_finishes_on_another_worker(stack):
    run_id = stack.submit("toy_sleep", seconds=3)
    victim = stack.container_of(run_id, "sleeper")
    docker("kill", victim)
    killed_at = time.monotonic()

    # Nobody tells the coordinator about the crash. The lease must run out,
    # the reaper must free the task, and the other worker must take it.
    stack.wait_for("the second lease",
                   """SELECT count(*) = 2 FROM task_events e JOIN tasks t ON t.id = e.task_id
                      WHERE t.run_id = %s AND e.event = 'LEASED'""", run_id, timeout=LEASE_S + 10)
    recovery = time.monotonic() - killed_at
    assert recovery < LEASE_S + 5, f"re-leased after {recovery:.1f}s, want under lease + 5s"

    stack.wait_for("the run to succeed", "SELECT state = 'SUCCEEDED' FROM runs WHERE id = %s", run_id)
    events = [(e, a) for e, a, _, _ in stack.events(run_id, "sleeper")]
    assert events == [("LEASED", 1), ("EXPIRED", 1), ("LEASED", 2), ("SUCCEEDED", 2)]
    new_owner = stack.one("SELECT worker_id FROM tasks WHERE run_id = %s", run_id)
    assert not new_owner.startswith(victim)
    assert stack.query("SELECT attempt FROM records WHERE run_id = %s", run_id) == [(2,)]
    print(f"\nrecovery after worker kill: {recovery:.1f}s (lease {LEASE_S:.0f}s)")


def test_frozen_worker_wakes_up_and_is_rejected(stack):
    run_id = stack.submit("toy_sleep", seconds=2)
    zombie = stack.container_of(run_id, "sleeper")
    docker("pause", zombie)

    # While the zombie is frozen, another worker takes the task and finishes.
    stack.wait_for("the run to succeed without the zombie",
                   "SELECT state = 'SUCCEEDED' FROM runs WHERE id = %s", run_id, timeout=LEASE_S + 20)
    assert stack.query("SELECT attempt FROM records WHERE run_id = %s", run_id) == [(2,)]

    # The zombie wakes up with an old fencing token. Two things can happen
    # first, and both are correct:
    #   - its task result is ready, it sends Complete and gets STALE_LEASE;
    #   - its heartbeat is answered "lease lost" and it kills the task.
    docker("unpause", zombie)
    stack.wait_for("the zombie to be rejected",
                   """SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id
                      WHERE t.run_id = %s AND e.event = 'STALE_REJECTED'""", run_id, timeout=15)
    time.sleep(1)  # give a wrong second write the chance to show up

    events = stack.events(run_id, "sleeper")
    assert [(e, a) for e, a, _, _ in events[:4]] == [
        ("LEASED", 1), ("EXPIRED", 1), ("LEASED", 2), ("SUCCEEDED", 2)]
    rejected = events[4:]
    assert rejected, "the zombie was never rejected"
    assert all(e == "STALE_REJECTED" and a == 1 for e, a, _, _ in rejected)
    assert {d["rpc"] for _, _, _, d in rejected} <= {"Complete", "Heartbeat"}
    # Whatever the zombie tried, the data is from attempt 2 only.
    assert stack.query("SELECT attempt FROM records WHERE run_id = %s", run_id) == [(2,)]
    assert stack.one("SELECT state FROM tasks WHERE run_id = %s", run_id) == "SUCCEEDED"
    print(f"\nzombie rejected via: {sorted(d['rpc'] for _, _, _, d in rejected)}")


def test_complete_with_old_fencing_token_gets_stale_lease(stack):
    """The Complete path of a zombie, without any race: this test is the
    zombie. It leases a task over gRPC, never heartbeats, lets a real worker
    finish the task, and then sends its own Complete with the old attempt."""
    from capitol_sdk import engine_pb2 as pb
    from capitol_sdk.client import EngineClient

    client = EngineClient(["localhost:50051", "localhost:50052"])
    run_id = f"it-zombie-{uuid.uuid4().hex[:8]}"
    # Type "sleeper" is known to the real workers, so they can take it later.
    client.submit_run(pb.SubmitRunRequest(run_id=run_id, workflow="toy_sleep", tasks=[
        pb.TaskSpec(type="sleeper", key="z", payload=b'{"seconds": 0.1}')]))

    # The real workers also want this task. If one of them wins the lease,
    # this round cannot prove anything, so the test is skipped, not passed.
    lease = client.lease("manual-zombie", ["sleeper"])
    if not lease.has_task or lease.run_id != run_id:
        pytest.skip("a real worker took the task first; nothing to prove in this round")

    stack.wait_for("a real worker to finish the task after the lease expired",
                   "SELECT state = 'SUCCEEDED' FROM runs WHERE id = %s", run_id, timeout=LEASE_S + 20)

    reply = client.complete(pb.CompleteRequest(
        task_id=lease.task_id, attempt=lease.attempt, output=b'{"from":"zombie"}',
        records=[pb.Record(kind="zombie_data", key="z", data=b"{}")]))
    assert reply.status == pb.CompleteResponse.STALE_LEASE

    events = [(e, a, (d or {}).get("rpc")) for e, a, _, d in stack.events(run_id, "sleeper")]
    assert events == [("LEASED", 1, None), ("EXPIRED", 1, None), ("LEASED", 2, None),
                      ("SUCCEEDED", 2, None), ("STALE_REJECTED", 1, "Complete")]
    assert stack.query("SELECT kind, attempt FROM records WHERE run_id = %s", run_id) == [("slept", 2)]
    assert stack.one("SELECT output FROM tasks WHERE run_id = %s", run_id) is None


def test_fatal_dead_letters_at_once_and_flaky_retries_with_growing_gaps(stack):
    run_id = stack.submit("toy_failures", fail_first=3)
    stack.wait_for("the run to end", "SELECT state <> 'RUNNING' FROM runs WHERE id = %s", run_id)

    states = dict(stack.query("SELECT type, state FROM tasks WHERE run_id = %s", run_id))
    assert states == {"always_fatal": "DEAD_LETTER", "flaky": "SUCCEEDED",
                      "after_fatal": "CANCELLED", "tolerant_join": "SUCCEEDED"}
    assert stack.one("SELECT state FROM runs WHERE id = %s", run_id) == "FAILED"

    fatal = [(e, a) for e, a, _, _ in stack.events(run_id, "always_fatal")]
    assert fatal == [("LEASED", 1), ("FAILED", 1), ("DEAD_LETTER", 1)]

    flaky = stack.events(run_id, "flaky")
    assert [(e, a) for e, a, _, _ in flaky] == [
        ("LEASED", 1), ("FAILED", 1), ("LEASED", 2), ("FAILED", 2),
        ("LEASED", 3), ("FAILED", 3), ("LEASED", 4), ("SUCCEEDED", 4)]
    waits = [detail["retry_in_ms"] for event, _, _, detail in flaky if event == "FAILED"]
    assert waits == sorted(waits) and waits[0] < waits[-1], f"backoff did not grow: {waits}"
    # The engine really waited: each new lease is later than failure + backoff.
    for i, (event, _, at, detail) in enumerate(flaky):
        if event == "FAILED":
            assert flaky[i + 1][2] >= at + timedelta(milliseconds=detail["retry_in_ms"])
    assert stack.query("SELECT kind FROM records WHERE run_id = %s ORDER BY kind", run_id) == [
        ("flaky_done",), ("join_done",)]
