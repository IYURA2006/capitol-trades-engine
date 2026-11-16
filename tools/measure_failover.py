"""Measure the gap a client sees when its coordinator is killed.

The client sends a Heartbeat every 20 ms through the SDK's failover client,
which starts on coordinator 1. After 2 s that coordinator is killed. The
gap is the longest time between two answered calls.

Usage: python tools/measure_failover.py [rounds]
"""
from __future__ import annotations

import itertools
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sdk")]

from capitol_sdk import engine_pb2 as pb
from capitol_sdk.client import EngineClient

VICTIM = "capitol-coordinator-1-1"


def one_round() -> float:
    client = EngineClient(["localhost:50051", "localhost:50052"])
    client._current = 0  # start on the coordinator that will be killed
    request = pb.HeartbeatRequest(task_id="failover-probe", attempt=1)
    client._call("Heartbeat", request, timeout=2)  # connect before the clock starts
    answered: list[float] = []
    stop = time.monotonic() + 6
    killer = threading.Timer(2.0, lambda: subprocess.run(["docker", "kill", VICTIM], check=False, capture_output=True))
    killer.start()
    while time.monotonic() < stop:
        client._call("Heartbeat", request, timeout=2)
        answered.append(time.monotonic())
        time.sleep(0.02)
    killer.join()
    subprocess.run(["docker", "start", VICTIM], capture_output=True, check=True)
    subprocess.run(["docker", "compose", "up", "-d", "--wait", "--no-recreate"], check=False, capture_output=True, cwd=ROOT)
    return max(b - a for a, b in itertools.pairwise(answered)) - 0.02


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    gaps = [one_round() * 1000 for _ in range(rounds)]
    print("gap per round, ms:", ", ".join(f"{g:.0f}" for g in gaps))
    print(f"min {min(gaps):.0f} ms, max {max(gaps):.0f} ms")


if __name__ == "__main__":
    main()
