# 002: Core engine

**Decision.** Postgres is the queue. A worker gets a task with `SELECT ... FOR UPDATE SKIP LOCKED`, and the same statement sets `attempt = attempt + 1`. That number is the fencing token. Every later write (`Heartbeat`, `Complete`, `Fail`) has the guard `WHERE id = $1 AND attempt = $2 AND state = 'LEASED'`. Zero rows updated means the caller is stale.

**Alternatives.** A separate queue (Redis, Kafka) next to the database, or a lock table with timestamps from the worker's clock.

**Why.** With one store, "task state changed" and "task left the queue" are the same transaction, so they cannot disagree after a crash. `SKIP LOCKED` lets 50 leasers work without waiting for each other. The guard needs no clocks: only Postgres `now()` decides when a lease expired.

**Three additions to the plan.**

- Runs are closed by the reaper (one idempotent `UPDATE`), not inside `Complete`. Two tasks that finish at the same moment cannot see each other, so neither could close the run safely.
- A dead-lettered parent releases children that tolerate failures and cancels the others; the cancel cascades.
- A retried `Fail` is recognised by its `FAILED` row in `task_events`, so it is counted once.
