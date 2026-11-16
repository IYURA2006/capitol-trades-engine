package store

// All SQL used by the engine lives in this file. Each guard names the
// design rule (docs/design.md) that it enforces.

// --- submit -----------------------------------------------------------------

const sqlInsertRun = `
INSERT INTO runs (id, workflow, snapshot_id, seed, state)
VALUES ($1, $2, NULLIF($3, ''), $4, 'RUNNING')
ON CONFLICT (id) DO NOTHING` // rule 5: a retried SubmitRun changes nothing

// Rule 6: ids are deterministic, so inserting the same task twice is a no-op.
// New tasks start PENDING; sqlSettleNewTasks sets the real state.
const sqlInsertTasks = `
INSERT INTO tasks (id, run_id, type, key, payload, state, tolerate_failures, max_attempts, rate_key)
SELECT u.id, $1, u.type, u.key, u.payload::jsonb, 'PENDING', u.tolerate,
       CASE WHEN u.max_attempts > 0 THEN u.max_attempts ELSE 5 END, NULLIF(u.rate_key, '')
FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::bool[], $7::int[], $8::text[])
     AS u(id, type, key, payload, tolerate, max_attempts, rate_key)
ON CONFLICT DO NOTHING
RETURNING id`

// Only tasks inserted by this statement's transaction get dependency rows.
const sqlInsertDeps = `
INSERT INTO task_deps (child_id, parent_id)
SELECT u.child, u.parent
FROM unnest($1::text[], $2::text[]) AS u(child, parent)
WHERE u.child = ANY($3::text[])
ON CONFLICT DO NOTHING`

const sqlEnsureRateKeys = `
INSERT INTO rate_limits (key, min_interval)
SELECT DISTINCT k, $2::interval FROM unnest($1::text[]) AS k
ON CONFLICT (key) DO NOTHING`

// Lock parents that existed before this transaction, in id order, so a
// parent cannot finish between our count and our commit.
const sqlLockTasks = `SELECT id FROM tasks WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE`

// A new task waits for every parent that is not yet terminal.
const sqlSettleNewTasks = `
UPDATE tasks t
SET pending_parents = s.n,
    state = CASE WHEN s.n = 0 THEN 'READY' ELSE 'PENDING' END
FROM (
  SELECT c.id, count(p.id) FILTER (WHERE p.state IN ('PENDING', 'READY', 'LEASED')) AS n
  FROM tasks c
  LEFT JOIN task_deps d ON d.child_id = c.id
  LEFT JOIN tasks p ON p.id = d.parent_id
  WHERE c.id = ANY($1::text[])
  GROUP BY c.id
) s
WHERE t.id = s.id`

// A new strict task whose parent already failed can never run.
const sqlCancelDoomedNewTasks = `
UPDATE tasks t SET state = 'CANCELLED'
WHERE t.id = ANY($1::text[]) AND NOT t.tolerate_failures
  AND EXISTS (
    SELECT 1 FROM task_deps d JOIN tasks p ON p.id = d.parent_id
    WHERE d.child_id = t.id AND p.state IN ('DEAD_LETTER', 'CANCELLED'))
RETURNING t.id, t.attempt`

// --- lease ------------------------------------------------------------------

// SKIP LOCKED lets many leasers pick different rows without waiting.
// attempt + 1 is the fencing token (rule 2). Time is Postgres time (rule 3).
// $4 is the rate key; NULL means "tasks without a rate key".
const sqlLease = `
WITH picked AS (
  SELECT id FROM tasks
  WHERE state = 'READY' AND run_after <= now()
    AND type = ANY($1::text[])
    AND rate_key IS NOT DISTINCT FROM $4::text
  ORDER BY run_after, id
  LIMIT 1
  FOR UPDATE SKIP LOCKED
), leased AS (
  UPDATE tasks t
  SET state = 'LEASED', attempt = t.attempt + 1, worker_id = $2,
      lease_expires_at = now() + $3::interval
  FROM picked WHERE t.id = picked.id
  RETURNING t.id, t.run_id, t.type, t.key, t.payload, t.attempt
), logged AS (
  INSERT INTO task_events (task_id, attempt, event, detail)
  SELECT id, attempt, 'LEASED', jsonb_build_object('worker', $2::text) FROM leased
)
SELECT l.id, l.type, l.key, l.payload, l.attempt, r.id, coalesce(r.snapshot_id, ''), r.seed
FROM leased l JOIN runs r ON r.id = l.run_id`

// One free rate key that has work for this worker. The row lock is the
// shared rate limiter: whoever holds it may lease one task for that key.
const sqlPickRateKey = `
SELECT rl.key FROM rate_limits rl
WHERE rl.next_free_at <= now()
  AND EXISTS (
    SELECT 1 FROM tasks t
    WHERE t.state = 'READY' AND t.rate_key = rl.key
      AND t.run_after <= now() AND t.type = ANY($1::text[]))
ORDER BY rl.next_free_at, rl.key
LIMIT 1
FOR UPDATE OF rl SKIP LOCKED`

const sqlAdvanceRateKey = `UPDATE rate_limits SET next_free_at = now() + min_interval WHERE key = $1`

// --- heartbeat --------------------------------------------------------------

// Rule 2: guard on state and attempt. 0 rows means the lease is lost.
const sqlHeartbeat = `
UPDATE tasks SET lease_expires_at = now() + $3::interval
WHERE id = $1 AND attempt = $2 AND state = 'LEASED'`

// --- complete ---------------------------------------------------------------

// Rule 2: only the current lease holder can finish the task.
const sqlComplete = `
UPDATE tasks SET state = 'SUCCEEDED', output = $3::jsonb, lease_expires_at = NULL
WHERE id = $1 AND attempt = $2 AND state = 'LEASED'
RETURNING run_id`

const sqlTaskStateAttempt = `SELECT state, attempt FROM tasks WHERE id = $1`

// Rule 4: records are written only here, in the Complete transaction.
const sqlInsertRecords = `
INSERT INTO records (run_id, kind, key, data, task_id, attempt)
SELECT $1, u.kind, u.key, u.data::jsonb, $2, $3
FROM unnest($4::text[], $5::text[], $6::text[]) AS u(kind, key, data)`

// Lock waiting children in id order first, so two parents that finish at
// the same moment cannot deadlock on their shared children.
const sqlLockPendingChildren = `
SELECT c.id FROM tasks c
WHERE c.state = 'PENDING'
  AND c.id IN (SELECT child_id FROM task_deps WHERE parent_id = ANY($1::text[]))
ORDER BY c.id
FOR UPDATE`

// One parent succeeded: its children wait for one parent fewer.
const sqlReleaseChildren = `
UPDATE tasks c
SET pending_parents = c.pending_parents - 1,
    state = CASE WHEN c.pending_parents - 1 = 0 THEN 'READY' ELSE c.state END
FROM task_deps d
WHERE d.parent_id = $1 AND c.id = d.child_id AND c.state = 'PENDING'`

// --- fail -------------------------------------------------------------------

const sqlLockLeased = `
SELECT failures, max_attempts FROM tasks
WHERE id = $1 AND attempt = $2 AND state = 'LEASED'
FOR UPDATE`

const sqlFailRetry = `
UPDATE tasks
SET state = 'READY', failures = failures + 1, last_error = $3,
    run_after = now() + $4::interval, lease_expires_at = NULL, worker_id = NULL
WHERE id = $1 AND attempt = $2 AND state = 'LEASED'`

const sqlFailDead = `
UPDATE tasks
SET state = 'DEAD_LETTER', failures = failures + 1, last_error = $3,
    lease_expires_at = NULL, worker_id = NULL
WHERE id = $1 AND attempt = $2 AND state = 'LEASED'`

const sqlHasEvent = `
SELECT EXISTS (SELECT 1 FROM task_events WHERE task_id = $1 AND attempt = $2 AND event = $3)`

// Parents failed: children that tolerate failures wait for fewer parents.
const sqlReleaseTolerantChildren = `
UPDATE tasks c
SET pending_parents = c.pending_parents - k.n,
    state = CASE WHEN c.pending_parents - k.n = 0 THEN 'READY' ELSE c.state END
FROM (
  SELECT child_id, count(*) AS n FROM task_deps
  WHERE parent_id = ANY($1::text[]) GROUP BY child_id
) k
WHERE c.id = k.child_id AND c.state = 'PENDING' AND c.tolerate_failures`

// Parents failed: strict children are cancelled. The caller repeats this
// for the cancelled children, so the cancel cascades down the DAG.
const sqlCancelStrictChildren = `
UPDATE tasks c SET state = 'CANCELLED'
WHERE c.state = 'PENDING' AND NOT c.tolerate_failures
  AND c.id IN (SELECT child_id FROM task_deps WHERE parent_id = ANY($1::text[]))
RETURNING c.id, c.attempt`

// --- reaper -----------------------------------------------------------------

// Rule 3: only Postgres decides that a lease has expired.
// An expiry is not a failure; only a very high count ($1) dead-letters.
const sqlReap = `
WITH expired AS (
  SELECT id FROM tasks
  WHERE state = 'LEASED' AND lease_expires_at < now()
  ORDER BY id
  FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
SET state = CASE WHEN t.expiries + 1 >= $1 THEN 'DEAD_LETTER' ELSE 'READY' END,
    expiries = t.expiries + 1, worker_id = NULL, lease_expires_at = NULL,
    last_error = CASE WHEN t.expiries + 1 >= $1 THEN 'too many lease expiries' ELSE t.last_error END
FROM expired e WHERE t.id = e.id
RETURNING t.id, t.attempt, t.state`

// A run is over when no task can still move. It failed if some task was
// cancelled, or a task dead-lettered and no child was there to tolerate it.
const sqlCloseRuns = `
UPDATE runs r
SET state = CASE WHEN EXISTS (
      SELECT 1 FROM tasks t
      WHERE t.run_id = r.id
        AND (t.state = 'CANCELLED'
             OR (t.state = 'DEAD_LETTER'
                 AND NOT EXISTS (SELECT 1 FROM task_deps d WHERE d.parent_id = t.id))))
    THEN 'FAILED' ELSE 'SUCCEEDED' END
WHERE r.state = 'RUNNING'
  AND NOT EXISTS (
    SELECT 1 FROM tasks t
    WHERE t.run_id = r.id AND t.state IN ('PENDING', 'READY', 'LEASED'))`

// --- events and reads -------------------------------------------------------

const sqlInsertEvent = `
INSERT INTO task_events (task_id, attempt, event, detail) VALUES ($1, $2, $3, $4::jsonb)`

const sqlInsertEvents = `
INSERT INTO task_events (task_id, attempt, event)
SELECT u.id, u.attempt, $3 FROM unnest($1::text[], $2::int[]) AS u(id, attempt)`

const sqlTaskRun = `SELECT run_id FROM tasks WHERE id = $1`

const sqlRunState = `SELECT state FROM runs WHERE id = $1`

const sqlStateCounts = `
SELECT type, state, count(*) FROM tasks WHERE run_id = $1 GROUP BY type, state ORDER BY type, state`

const sqlSetRateLimit = `
INSERT INTO rate_limits (key, min_interval) VALUES ($1, $2::interval)
ON CONFLICT (key) DO UPDATE SET min_interval = EXCLUDED.min_interval`

// Metrics only: how long ago this attempt was leased.
const sqlTaskDuration = `
SELECT t.type, extract(epoch FROM now() - e.at)::float8
FROM task_events e JOIN tasks t ON t.id = e.task_id
WHERE e.task_id = $1 AND e.attempt = $2 AND e.event = 'LEASED'
LIMIT 1`

const sqlActiveCounts = `
SELECT state, count(*) FROM tasks
WHERE state IN ('PENDING', 'READY', 'LEASED') GROUP BY state`

// --- status page (read only) ------------------------------------------------

const sqlStatusRuns = `
SELECT id, workflow, state, coalesce(snapshot_id, 'live'), to_char(created_at, 'YYYY-MM-DD HH24:MI:SS')
FROM runs ORDER BY created_at DESC LIMIT $1`

const sqlStatusCounts = `
SELECT type, state, count(*) FROM tasks WHERE run_id = $1 GROUP BY type, state ORDER BY type, state`

const sqlStatusEvents = `
SELECT to_char(e.at, 'HH24:MI:SS.MS'), t.type, t.key, e.attempt, e.event, coalesce(e.detail::text, '')
FROM task_events e JOIN tasks t ON t.id = e.task_id
WHERE t.run_id = $1 ORDER BY e.seq DESC LIMIT $2`

const sqlStatusDeadLetters = `
SELECT type, key, attempt, failures, expiries, coalesce(left(last_error, 200), '')
FROM tasks WHERE run_id = $1 AND state = 'DEAD_LETTER' ORDER BY type, key LIMIT $2`
