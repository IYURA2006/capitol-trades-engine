"""Helpers for tests that drive the real Docker Compose stack.

Start the stack with `make test-integration`; it sets a short lease so the
tests finish quickly.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid

import psycopg
import pytest

DSN = os.environ.get("TEST_DSN", "postgresql://engine:engine@localhost:5433/engine")
LEASE_S = float(os.environ.get("TEST_LEASE_S", "6"))


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


class Stack:
    def __init__(self, conn):
        self.conn = conn

    def submit(self, workflow: str, **params) -> str:
        run_id = f"it-{workflow}-{uuid.uuid4().hex[:8]}"
        cmd = [".venv/bin/python", "-m", "capitol_sdk.cli", "submit", "--workflow", workflow, "--run", run_id]
        for name, value in params.items():
            cmd += ["--param", f"{name}={value}"]
        env = {**os.environ, "PYTHONPATH": "sdk:."}
        subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        return run_id

    def query(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        rows = self.query(sql, *args)
        return rows[0][0] if rows else None

    def wait_for(self, what: str, sql: str, *args, timeout: float = 60):
        """Poll until the query returns a true value. Polling the database is
        the only way to observe another process; it never changes the result."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.one(sql, *args)
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    def events(self, run_id: str, task_type: str):
        return self.query(
            """SELECT e.event, e.attempt, e.at, e.detail FROM task_events e
               JOIN tasks t ON t.id = e.task_id
               WHERE t.run_id = %s AND t.type = %s ORDER BY e.seq""", run_id, task_type)

    def container_of(self, run_id: str, task_type: str) -> str:
        """The container that holds the lease. worker_id starts with the
        container's hostname, which is its short id."""
        worker_id = self.wait_for(
            "a worker to lease the task",
            "SELECT worker_id FROM tasks WHERE run_id = %s AND type = %s AND state = 'LEASED'", run_id, task_type)
        return worker_id.rsplit("-", 1)[0]


@pytest.fixture()
def stack():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield Stack(conn)
    # Bring back any worker a test killed or froze.
    for cid in docker("ps", "-aq", "--filter", "label=com.docker.compose.service=worker",
                      "--filter", "label=com.docker.compose.project=capitol").split():
        state = docker("inspect", "-f", "{{.State.Status}}", cid)
        if state == "paused":
            docker("unpause", cid)
        elif state != "running":
            docker("start", cid)
