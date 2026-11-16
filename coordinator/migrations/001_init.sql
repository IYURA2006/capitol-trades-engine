-- Rule 1: Postgres is the single source of truth. Everything the engine
-- needs after a restart lives in these tables.

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  workflow    TEXT NOT NULL,
  snapshot_id TEXT,                 -- NULL = live mode
  seed        BIGINT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
  id                TEXT PRIMARY KEY,          -- rule 6: sha256(run_id, type, key)
  run_id            TEXT NOT NULL REFERENCES runs(id),
  type              TEXT NOT NULL,
  key               TEXT NOT NULL,
  payload           JSONB NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                      ('PENDING', 'READY', 'LEASED', 'SUCCEEDED', 'DEAD_LETTER', 'CANCELLED')),
  pending_parents   INT  NOT NULL DEFAULT 0,
  tolerate_failures BOOL NOT NULL DEFAULT false,
  attempt           INT  NOT NULL DEFAULT 0,   -- fencing token, +1 on every lease
  failures          INT  NOT NULL DEFAULT 0,   -- explicit Fail calls only
  expiries          INT  NOT NULL DEFAULT 0,   -- lease expiries only
  max_attempts      INT  NOT NULL DEFAULT 5,
  run_after         TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_expires_at  TIMESTAMPTZ,
  worker_id         TEXT,
  rate_key          TEXT,
  output            JSONB,
  last_error        TEXT,
  UNIQUE (run_id, type, key)
);
CREATE INDEX tasks_ready  ON tasks (run_after, id)    WHERE state = 'READY';
CREATE INDEX tasks_leased ON tasks (lease_expires_at) WHERE state = 'LEASED';
CREATE INDEX tasks_active ON tasks (run_id)           WHERE state IN ('PENDING', 'READY', 'LEASED');

CREATE TABLE task_deps (
  child_id  TEXT NOT NULL REFERENCES tasks(id),
  parent_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX task_deps_parent ON task_deps (parent_id);

-- Domain output. Rule 4: written only inside the Complete transaction.
CREATE TABLE records (
  run_id  TEXT NOT NULL,
  kind    TEXT NOT NULL,
  key     TEXT NOT NULL,
  data    JSONB NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  attempt INT  NOT NULL,
  PRIMARY KEY (run_id, kind, key)
);

-- Append-only log. The invariant checker reads this.
CREATE TABLE task_events (
  seq     BIGSERIAL PRIMARY KEY,
  task_id TEXT NOT NULL,
  attempt INT  NOT NULL,
  event   TEXT NOT NULL,  -- LEASED SUCCEEDED FAILED EXPIRED STALE_REJECTED DEAD_LETTER CANCELLED
  at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail  JSONB
);
CREATE INDEX task_events_task ON task_events (task_id, seq);

-- One row per rate key, shared by every coordinator and worker.
CREATE TABLE rate_limits (
  key          TEXT PRIMARY KEY,
  min_interval INTERVAL NOT NULL,
  next_free_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
