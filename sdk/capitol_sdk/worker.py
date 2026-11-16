"""The worker loop.

For each leased task the worker starts a child process to run the task
function. The parent keeps two jobs for itself:

  * a heartbeat thread, which extends the lease every third of its length;
  * waiting for the child, with a timeout.

If the heartbeat says the lease is lost, or the task runs too long, the
parent kills the child. The worker never writes to Postgres: the result
travels to the coordinator in Complete (design rule 4).

Run it with:  python -m capitol_sdk.worker
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import socket
import threading
import time
import uuid

import grpc

from . import engine_pb2 as pb
from .client import EngineClient, Stopped
from .context import BlobStore, Context, PostgresRecords, loads
from .registry import TASKS, load_modules
from .runner import Outcome, execute

log = logging.getLogger("capitol.worker")


def _child_main(conn, modules: list[str], job: dict, settings: dict) -> None:
    """Entry point of the child process."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent decides when we stop
    try:
        load_modules(modules)
        from .http import make_http

        http = make_http(job["snapshot_id"], settings)
        ctx = Context(
            run_id=job["run_id"], task_type=job["task_type"], key=job["key"], attempt=job["attempt"],
            seed=job["seed"], snapshot_id=job["snapshot_id"],
            blobs=BlobStore(settings["blob_dir"]), records=PostgresRecords(settings["records_dsn"]), http=http,
        )
        outcome = execute(ctx, loads(job["payload"]))
    except BaseException as err:  # noqa: BLE001 - e.g. a module that fails to import
        outcome = Outcome("retry", error=f"worker child: {type(err).__name__}: {err}")
    conn.send(outcome)
    conn.close()


class Worker:
    def __init__(self, client: EngineClient, modules: list[str], settings: dict, worker_id: str | None = None):
        self.client = client
        self.modules = modules
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.stop = threading.Event()
        # forkserver: children are forked from a clean helper process that has
        # the task modules loaded, but no gRPC threads. Fast and safe.
        self.mp = mp.get_context("forkserver")
        self.mp.set_forkserver_preload(["capitol_sdk.runner", *modules])
        load_modules(modules)
        self.task_types = sorted(TASKS)

    def run_forever(self) -> None:
        log.info("worker %s ready for %s", self.worker_id, self.task_types)
        while not self.stop.is_set():
            try:
                lease = self.client.lease(self.worker_id, self.task_types, stop=self.stop)
            except Stopped:
                break
            except grpc.RpcError as err:
                log.error("lease failed: %s", err)
                self.stop.wait(1)
                continue
            if lease.has_task:
                self.run_task(lease)

    def run_task(self, lease) -> None:
        definition = TASKS[lease.task_type]
        lost = threading.Event()  # set when the coordinator says the lease is gone
        done = threading.Event()  # set when Complete or Fail has been answered
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(lease, lost, done), name="heartbeat", daemon=True)

        job = {
            "run_id": lease.run_id, "task_type": lease.task_type, "key": lease.task_key,
            "attempt": lease.attempt, "seed": lease.seed, "snapshot_id": lease.snapshot_id,
            "payload": bytes(lease.payload),
        }
        parent_conn, child_conn = self.mp.Pipe(duplex=False)
        child = self.mp.Process(target=_child_main, args=(child_conn, self.modules, job, self.settings), daemon=True)
        child.start()
        child_conn.close()
        heartbeat.start()

        outcome: Outcome | None = None
        deadline = time.monotonic() + definition.timeout_s
        try:
            while True:
                if parent_conn.poll(0.05):
                    try:
                        outcome = parent_conn.recv()
                    except EOFError:
                        outcome = Outcome("retry", error="task process died without a result")
                    break
                if lost.is_set():
                    log.warning("lease lost for %s/%s attempt %d; killing the task",
                                lease.task_type, lease.task_key, lease.attempt)
                    break
                if time.monotonic() > deadline:
                    outcome = Outcome("retry", error=f"timeout after {definition.timeout_s}s")
                    break
                if not child.is_alive() and not parent_conn.poll(0):
                    outcome = Outcome("retry", error=f"task process exited with code {child.exitcode}")
                    break
        finally:
            if child.is_alive():
                child.kill()
            child.join(timeout=5)
            parent_conn.close()

        try:
            if outcome is not None:
                self._report(lease, outcome, lost)
        finally:
            done.set()
            heartbeat.join(timeout=5)

    def _heartbeat_loop(self, lease, lost: threading.Event, done: threading.Event) -> None:
        interval = max(lease.lease_seconds / 3, 0.2)
        while not done.wait(interval):
            try:
                reply = self.client.heartbeat_once(lease.task_id, lease.attempt)
            except grpc.RpcError:
                continue  # coordinators unreachable; Postgres time decides the lease
            if reply.lease_lost:
                lost.set()
                return

    def _report(self, lease, outcome: Outcome, lost: threading.Event) -> None:
        """Send Complete or Fail. The coordinator's guard has the last word."""
        name = f"{lease.task_type}/{lease.task_key} attempt {lease.attempt}"
        try:
            if outcome.status == "ok":
                request = pb.CompleteRequest(
                    task_id=lease.task_id, attempt=lease.attempt, output=outcome.output,
                    records=[pb.Record(kind=k, key=key, data=data) for k, key, data in outcome.records],
                    sub_dag=[pb.TaskSpec(
                        type=s["type"], key=s["key"], payload=s["payload"], depends_on_keys=s["depends_on"],
                        rate_key=s["rate_key"], tolerate_failures=s["tolerate_failures"],
                        max_attempts=s["max_attempts"]) for s in outcome.sub_dag],
                )
                try:
                    reply = self.client.complete(request, stop=self.stop)
                except grpc.RpcError as err:
                    # The coordinator refused the result itself (bad sub-DAG,
                    # duplicate record key). A retry would give the same answer.
                    log.error("%s: result rejected: %s", name, err.details())
                    outcome = Outcome("fatal", error=f"result rejected: {err.details()}")
                else:
                    if reply.status == pb.CompleteResponse.STALE_LEASE:
                        log.warning("%s: STALE_LEASE on Complete; result dropped", name)
                    return
            reply = self.client.fail(pb.FailRequest(
                task_id=lease.task_id, attempt=lease.attempt, error=outcome.error,
                fatal=outcome.status == "fatal", retry_after_seconds=outcome.retry_after_s), stop=self.stop)
            if reply.status == pb.FailResponse.STALE_LEASE:
                log.warning("%s: STALE_LEASE on Fail", name)
        except Stopped:
            pass
        except grpc.RpcError as err:
            log.error("%s: could not report: %s", name, err)


def settings_from_env() -> dict:
    return {
        "blob_dir": os.environ.get("BLOB_DIR", "blobs"),
        "snapshot_dir": os.environ.get("SNAPSHOT_DIR", "snapshots"),
        "records_dsn": os.environ.get(
            "RECORDS_DSN", "postgresql://worker_ro:worker_ro@localhost:5433/engine"),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    addresses = os.environ.get("COORDINATORS", "localhost:50051,localhost:50052").split(",")
    modules = [m for m in os.environ.get("TASK_MODULES", "pipeline.tasks.toy").split(",") if m]
    worker = Worker(EngineClient(addresses), modules, settings_from_env())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.run_forever()


if __name__ == "__main__":
    main()
