"""Measure engine throughput with real worker containers.

For each worker count it scales the Compose stack, runs the toy workflow
with no sleep, and reads the result from task_events:

    tasks per second = leaves / (last leaf SUCCEEDED - first leaf LEASED)

Usage: python tools/bench_throughput.py [--n 3000] [--workers 1,4,16]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
import uuid

import psycopg

DSN = os.environ.get("TEST_DSN", "postgresql://engine:engine@localhost:5433/engine")


def sh(*cmd: str, **env: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True, env={**os.environ, **env})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3000)
    parser.add_argument("--workers", default="1,4,16")
    args = parser.parse_args()

    print("| Workers | Tasks | Seconds | Tasks per second |")
    print("| --- | --- | --- | --- |")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for workers in [int(w) for w in args.workers.split(",")]:
            sh("docker", "compose", "up", "-d", "--wait", "--scale", f"worker={workers}", "--no-recreate")
            time.sleep(2)  # let new workers connect before the clock starts
            run_id = f"bench-{workers}-{uuid.uuid4().hex[:6]}"
            sh(".venv/bin/python", "-m", "capitol_sdk.cli", "submit", "--workflow", "toy", "--run", run_id,
               "--param", f"n={args.n}", "--param", "max_sleep_ms=0", PYTHONPATH="sdk:.")
            while conn.execute("SELECT state FROM runs WHERE id = %s", (run_id,)).fetchone()[0] == "RUNNING":
                time.sleep(0.5)
            leaves, seconds = conn.execute(
                """SELECT count(DISTINCT t.id),
                          extract(epoch FROM max(e.at) FILTER (WHERE e.event = 'SUCCEEDED')
                                           - min(e.at) FILTER (WHERE e.event = 'LEASED'))
                   FROM task_events e JOIN tasks t ON t.id = e.task_id
                   WHERE t.run_id = %s AND t.type = 'hash_leaf'""", (run_id,)).fetchone()
            seconds = float(seconds)
            print(f"| {workers} | {leaves} | {seconds:.1f} | {leaves / seconds:.0f} |", flush=True)


if __name__ == "__main__":
    main()
