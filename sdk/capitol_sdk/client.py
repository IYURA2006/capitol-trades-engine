"""gRPC client that fails over between coordinators.

Every RPC is safe to retry (design rule 5), so when a call fails with an
unknown outcome the client simply sends it again, maybe to the other
coordinator.
"""
from __future__ import annotations

import random
import threading
import time

import grpc

from . import engine_pb2 as pb
from . import engine_pb2_grpc as pb_grpc

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
RETRYABLE = {
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.CANCELLED,
    grpc.StatusCode.UNKNOWN,
    grpc.StatusCode.INTERNAL,
}


class Stopped(Exception):
    """The caller asked the client to stop retrying."""


class EngineClient:
    def __init__(self, addresses: list[str]):
        if not addresses:
            raise ValueError("at least one coordinator address is required")
        options = [
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ]
        self._stubs = [pb_grpc.EngineStub(grpc.insecure_channel(a, options=options)) for a in addresses]
        self._lock = threading.Lock()
        self._current = random.randrange(len(self._stubs))  # spread workers over coordinators

    def _call(self, method: str, request, timeout: float, stop: threading.Event | None = None):
        """Call until one coordinator answers. Raises grpc.RpcError only for
        errors that a retry cannot fix (for example INVALID_ARGUMENT)."""
        delay = 0.1
        failures = 0
        while True:
            with self._lock:
                index = self._current
            try:
                return getattr(self._stubs[index], method)(request, timeout=timeout)
            except grpc.RpcError as err:
                if err.code() not in RETRYABLE:
                    raise
            failures += 1
            with self._lock:
                if self._current == index:
                    self._current = (index + 1) % len(self._stubs)
            # Try the other coordinator at once; back off only after a full round.
            if failures % len(self._stubs) == 0:
                wait = delay * (0.5 + random.random() / 2)
                if stop is not None:
                    if stop.wait(wait):
                        raise Stopped()
                else:
                    time.sleep(wait)
                delay = min(delay * 2, 2.0)
            elif stop is not None and stop.is_set():
                raise Stopped()

    def submit_run(self, request: pb.SubmitRunRequest) -> pb.SubmitRunResponse:
        return self._call("SubmitRun", request, timeout=60)

    def lease(self, worker_id: str, task_types: list[str], stop: threading.Event | None = None) -> pb.LeaseResponse:
        return self._call("LeaseTask", pb.LeaseRequest(worker_id=worker_id, task_types=task_types), timeout=25, stop=stop)

    def heartbeat_once(self, task_id: str, attempt: int) -> pb.HeartbeatResponse:
        """One try on each coordinator, no waiting. The heartbeat loop calls
        this again soon, so a long retry here would only hide a lost lease."""
        request = pb.HeartbeatRequest(task_id=task_id, attempt=attempt)
        last: Exception | None = None
        for _ in self._stubs:
            with self._lock:
                index = self._current
            try:
                return self._stubs[index].Heartbeat(request, timeout=3)
            except grpc.RpcError as err:
                last = err
                with self._lock:
                    if self._current == index:
                        self._current = (index + 1) % len(self._stubs)
        raise last  # type: ignore[misc]

    def complete(self, request: pb.CompleteRequest, stop: threading.Event | None = None) -> pb.CompleteResponse:
        return self._call("Complete", request, timeout=30, stop=stop)

    def fail(self, request: pb.FailRequest, stop: threading.Event | None = None) -> pb.FailResponse:
        return self._call("Fail", request, timeout=10, stop=stop)
