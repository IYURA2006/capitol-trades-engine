# Design

## Problem

Run a DAG of tasks on many workers so that each task's effects are committed exactly once, while any process can die at any moment: workers, coordinators and the database itself.

The hard cases are not clean crashes. They are:

- a **zombie**: a worker that freezes, loses its lease, and later wakes up and tries to finish;
- an **unknown outcome**: a request was sent, the connection broke, and the caller cannot know if it was applied;
- a **half-done step**: a process dies between two writes that belong together.

## Design rules

1. Postgres is the single source of truth. Coordinators keep nothing in memory that matters.
2. Every state change is one transaction guarded by `state` and `attempt`. Zero rows updated means the caller is stale.
3. Time comes from Postgres `now()` only.
4. Workers never write to Postgres. Outputs commit inside the coordinator's `Complete` transaction.
5. Every RPC is safe to retry.
6. Task ids are deterministic: `sha256(run_id, type, key)`; inserts use `ON CONFLICT DO NOTHING`.
7. Tasks are pure functions of their payload and the snapshot.
8. Files are content-addressed: a blob's name is its SHA-256.

## Task lifecycle

`PENDING` (waiting for parents) → `READY` → `LEASED` → `SUCCEEDED`, `DEAD_LETTER` or `CANCELLED`.

- **Lease.** `SELECT ... FOR UPDATE SKIP LOCKED` picks one `READY` row; the same statement sets `LEASED` and `attempt = attempt + 1`. That number is the fencing token.
- **Heartbeat.** Extends `lease_expires_at`, guarded by the token.
- **Complete.** One transaction: mark `SUCCEEDED` (guarded), insert the task's records, insert its sub-DAG, release its children.
- **Fail.** Retry with backoff `min(cap, base * 2^failures) * random[0.5, 1.0]`, or dead-letter when `failures` reaches `max_attempts` or the error is fatal.
- **Reaper.** Every coordinator, every 2 s: leases past their time go back to `READY` and `expiries + 1`. An expiry is not a failure, so chaos alone cannot dead-letter a healthy task (a cap of 20 expiries catches a task that crashes every worker). The reaper also closes finished runs.
- **Dead letter.** Children that need the task are `CANCELLED`, and that cascades. A join marked `tolerate_failures` still runs.

## Failure cases and what handles them

| Failure | What happens | Why it is safe |
| --- | --- | --- |
| Worker killed mid-task | Lease runs out, reaper frees the task, another worker leases it with `attempt + 1` | The dead worker wrote nothing: outputs only travel in `Complete` |
| Worker frozen, wakes up later (zombie) | Its `Complete` or `Heartbeat` matches zero rows → `STALE_LEASE`; it drops the result | Guard on `attempt` (rule 2) |
| `Complete` sent, answer lost | Worker sends it again, maybe to the other coordinator; the row is already `SUCCEEDED` with the same attempt → OK, nothing changes | Rule 5 |
| Coordinator killed mid-transaction | Postgres rolls the transaction back; the worker retries on the other coordinator | Rule 1: no state outside the database |
| Coordinator killed while inserting 10,001 children | None of them is visible; the retry inserts all of them | Sub-DAG and parent state commit together |
| Both coordinators down | Workers back off and keep trying; leases may expire and are redone | Leases, not connections, define ownership |
| Postgres restarts | Open transactions are lost; coordinators reconnect; workers retry | Rules 2 and 5 |
| Postgres stalls | RPCs time out and are retried | Rule 5 |
| Two parents finish at the same instant | Both lock their shared children in id order, then decrement | No lost update, no deadlock; `40P01` and `40001` are retried |
| Task writes a record key twice | Whole `Complete` fails; no partial output | One transaction |
| Worker killed while writing a blob | Temp file is left; the real name appears only after `rename` | Rule 8 plus atomic rename; hash checked on read |
| 20 workers, one polite rate | One row per rate key; a leaser must lock it and moves `next_free_at` | The lock is the limiter, shared by all coordinators |

## How correctness is checked

A chaos run passes only if seven invariants hold and the results equal a single-process reference (`run_local`, same workflow, snapshot and seed):

1. Every task and the run are in a terminal state.
2. A task is first leased only after each parent has ended (and, for strict tasks, succeeded).
3. Every task has at most one `SUCCEEDED` event.
4. Every record's `attempt` equals its task's final attempt.
5. The set of tasks and their final states equals the reference.
6. All records equal the reference (`jsonb` equality, and a hash of the canonical text).
7. For each rate key, no two leases are closer than `min_interval`.

A run only counts if it contained a worker kill, a zombie, a coordinator kill and a Postgres restart while it was working, and at least one rejected stale request. The checker has its own test: one correct run is damaged in 15 ways and each must be reported.

What this does **not** prove: that no bad schedule exists. It is strong evidence over many seeded schedules, not a proof.

## Measured numbers

Laptop: Apple Silicon, 10 cores, everything in Docker Desktop.

| What | Value | Where |
| --- | --- | --- |
| Throughput, 1 / 4 / 16 workers | 24 / 89 / 159 tasks per second | `tools/bench_throughput.py`, 2,000 near-empty tasks |
| Overhead per task, one worker | about 41 ms | same |
| Insert 10,001 tasks + 10,000 dependencies in one `Complete` | 0.6 s | `TestFanOutTenThousandThenJoin` |
| Task leased again after its worker is killed | 6.2 to 6.8 s (lease 6 s, reaper 1 s) | `test_killed_worker_task_finishes_on_another_worker` |
| Gap a client sees when its coordinator is killed | 15 to 47 ms over 5 kills (the client tries the other coordinator at once) | `tools/measure_failover.py` |
| Smallest gap between leases of one rate key (limit 200 ms, 20 leasers) | 200.0 ms | `TestRateLimitIsSharedByAllLeasers` |
| Children visible after a coordinator is killed mid-insert | 0 of 10,001 | `test_coordinator_killed_while_inserting_the_children` |
| Chaos, toy workflow | 100 of 100 runs match; 703 faults; 464 lease expiries; 429 stale requests rejected | `docs/chaos-results.md` |
| Chaos, real pipeline (3,153 tasks per run) | 100 of 100 runs match; 703 faults; 434 lease expiries; 438 stale requests rejected | `docs/chaos-results.md` |
| Pipeline | 966 filings, 843 parsed, 123 skipped; 8,957 trades, 8,682 studied | `trades_summary`, `study_summary` records |

Throughput is almost linear from 1 to 4 workers (3.7x). At 16 workers it is 6.6x: the machine has 10 cores and also runs Postgres and two coordinators.

## What was cut, and why

| Cut | Why |
| --- | --- |
| Leader election, epochs | Coordinators are stateless, so nothing needs a leader |
| In-memory rate limiter | Wrong with two coordinators; replaced by one Postgres row |
| Workers writing results | A zombie could write data that the task-row guard does not see |
| OCR | Scanned filings (12.7%) are counted and skipped |
| Senate data | Access agreement and blocked cloud addresses: risk without reward |
| Market model, multiple-comparison correction, fuzzy ticker matching | More finance machinery than the claim needs; replaced by market-adjusted return, few fixed comparisons, a regex |
| Tracing, Kubernetes, object store, network-fault proxy | Each adds a tool and nothing to the correctness claim |

## Limits and what would change first

- **Postgres is the single point of failure.** In production: a replicated, managed Postgres with automatic failover. The engine's guards do not change.
- **Fan-in is serial.** Each finishing parent locks the join row to decrement `pending_parents`; about 520 completes per second measured. First fix: let the reaper promote joins in batches.
- **The lease query slows down inside a big run** (8 ms average in a 10,000-task run), because finished tasks leave dead entries in the `READY` index until vacuum. First fix: partition `tasks` by run; later, a log-based queue.
- **Polling.** Workers poll every 250 ms when idle. `LISTEN/NOTIFY` would cut that delay.
- **Outputs pass through the coordinator** and must stay small (16 MB message limit). Large data goes to the blob store by hash.
- **The blob store is a local folder.** With workers on several machines it would be an object store; the content-addressed names stay the same.
