# 003: Python SDK and failure handling

**Decision.** Each task runs in a child process. The parent only waits for the result and runs a heartbeat thread (every third of the lease). It kills the child on timeout or when the heartbeat answers "lease lost". Children come from a `forkserver`, a clean helper process with the task modules already loaded.

**Alternatives.** Run tasks as threads in the worker (cannot be killed, a stuck task blocks the worker), or `fork` straight from the worker (unsafe: gRPC has background threads).

**Why.** A process can always be killed, so one bad PDF cannot hang a worker. The task function sees only `ctx` and `payload`, so `run_local` can call the same function without a coordinator and produce the reference result.

**Failure rules.**

- `Fatal` goes straight to dead letter. Any other exception is retried with backoff `min(cap, base * 2^failures)` times a random factor in [0.5, 1.0].
- A lease expiry adds to `expiries`, not `failures`. A killed worker is not the task's fault.
- The client tries the other coordinator at once and backs off only after a full round. This is safe because every RPC can be repeated.

**Changed from the plan.** A zombie can be stopped by `Complete` or by `Heartbeat`; both are logged (see `docs/bugs.md`, entry 1).
