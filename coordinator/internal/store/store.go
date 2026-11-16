// Package store is the only package that talks to Postgres.
//
// Every exported method is one transaction (design rule 2), and every
// method is safe to call again after an unknown outcome (rule 5).
package store

import (
	"context"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Outcome tells a worker whether its lease was still valid.
type Outcome int

const (
	OK Outcome = iota
	Stale
	// Dead is OK for the caller, and also says the task was dead-lettered.
	Dead
)

// ErrDuplicateRecord means a task tried to write a record key that another
// task in the same run already wrote. That is a bug in the pipeline.
var ErrDuplicateRecord = errors.New("record key already exists in this run")

// ErrNotFound means the task or run id is unknown.
var ErrNotFound = errors.New("not found")

type Config struct {
	// MaxExpiries dead-letters a task that keeps losing its lease.
	MaxExpiries int
	// DefaultRateInterval is used for a rate key nobody configured.
	DefaultRateInterval time.Duration
	// OnTaskDone, if set, is called after a successful Complete with the
	// time since the task was leased. Used for metrics only.
	OnTaskDone func(taskType string, sinceLease time.Duration)
}

type Store struct {
	pool *pgxpool.Pool
	cfg  Config
}

func New(pool *pgxpool.Pool, cfg Config) *Store {
	if cfg.MaxExpiries <= 0 {
		cfg.MaxExpiries = 20
	}
	if cfg.DefaultRateInterval <= 0 {
		cfg.DefaultRateInterval = 2 * time.Second
	}
	return &Store{pool: pool, cfg: cfg}
}

type Run struct {
	ID         string
	Workflow   string
	SnapshotID string
	Seed       int64
}

// NewTask is a validated task ready to insert. ParentIDs are task ids.
type NewTask struct {
	ID               string
	Type             string
	Key              string
	Payload          []byte // JSON
	RateKey          string
	TolerateFailures bool
	MaxAttempts      int
	ParentIDs        []string
}

type Record struct {
	Kind string
	Key  string
	Data []byte // JSON
}

type Leased struct {
	TaskID     string
	Type       string
	Key        string
	Payload    []byte
	Attempt    int64
	RunID      string
	SnapshotID string
	Seed       int64
}

// inTx runs fn in a transaction and retries it on deadlock or
// serialization errors. fn must be safe to run more than once.
func (s *Store) inTx(ctx context.Context, fn func(tx pgx.Tx) error) error {
	for try := 0; ; try++ {
		err := pgx.BeginFunc(ctx, s.pool, fn)
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && (pgErr.Code == "40P01" || pgErr.Code == "40001") && try < 10 {
			time.Sleep(time.Duration(5+rand.IntN(20)) * time.Millisecond)
			continue
		}
		return err
	}
}

// SubmitRun creates a run and its tasks. If the run id already exists it
// changes nothing and returns created = false.
func (s *Store) SubmitRun(ctx context.Context, run Run, tasks []NewTask) (created bool, err error) {
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, sqlInsertRun, run.ID, run.Workflow, run.SnapshotID, run.Seed)
		if err != nil {
			return err
		}
		created = tag.RowsAffected() == 1
		if !created {
			return nil
		}
		return insertDAG(ctx, tx, s.cfg, run.ID, tasks)
	})
	return created, err
}

// insertDAG inserts tasks and their dependencies and sets each new task to
// READY or PENDING. Used for a new run and for a sub-DAG in Complete.
func insertDAG(ctx context.Context, tx pgx.Tx, cfg Config, runID string, tasks []NewTask) error {
	if len(tasks) == 0 {
		return nil
	}
	n := len(tasks)
	ids, types, keys, payloads := make([]string, n), make([]string, n), make([]string, n), make([]string, n)
	tolerate, maxAttempts, rateKeys := make([]bool, n), make([]int32, n), make([]string, n)
	var depChild, depParent, usedRateKeys []string
	for i, t := range tasks {
		ids[i], types[i], keys[i] = t.ID, t.Type, t.Key
		payloads[i] = jsonOrEmptyObject(t.Payload)
		tolerate[i], maxAttempts[i], rateKeys[i] = t.TolerateFailures, int32(t.MaxAttempts), t.RateKey
		if t.RateKey != "" {
			usedRateKeys = append(usedRateKeys, t.RateKey)
		}
		for _, p := range t.ParentIDs {
			depChild = append(depChild, t.ID)
			depParent = append(depParent, p)
		}
	}

	if len(usedRateKeys) > 0 {
		if _, err := tx.Exec(ctx, sqlEnsureRateKeys, usedRateKeys, cfg.DefaultRateInterval); err != nil {
			return err
		}
	}

	inserted, err := queryStrings(ctx, tx, sqlInsertTasks, runID, ids, types, keys, payloads, tolerate, maxAttempts, rateKeys)
	if err != nil {
		return err
	}
	if len(inserted) == 0 {
		return nil
	}
	if len(depChild) > 0 {
		if _, err := tx.Exec(ctx, sqlInsertDeps, depChild, depParent, inserted); err != nil {
			return err
		}
	}

	// Usually every task is new. If some id already existed (two parents
	// emitted the same child), new tasks may depend on an old task, so lock
	// the old ones before counting unfinished parents.
	someExisted := len(inserted) < n
	if someExisted {
		isNew := make(map[string]bool, len(inserted))
		for _, id := range inserted {
			isNew[id] = true
		}
		var old []string
		for _, id := range ids {
			if !isNew[id] {
				old = append(old, id)
			}
		}
		if _, err := tx.Exec(ctx, sqlLockTasks, old); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(ctx, sqlSettleNewTasks, inserted); err != nil {
		return err
	}
	if someExisted {
		doomed, err := queryIDAttempts(ctx, tx, sqlCancelDoomedNewTasks, inserted)
		if err != nil {
			return err
		}
		return cancelAndCascade(ctx, tx, doomed)
	}
	return nil
}

// Lease hands one READY task to a worker, or returns nil if there is none.
func (s *Store) Lease(ctx context.Context, workerID string, types []string, lease time.Duration) (*Leased, error) {
	if len(types) == 0 {
		return nil, nil
	}
	// Rate-limited tasks first: they are the scarce ones.
	var out *Leased
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		out = nil
		var key string
		err := tx.QueryRow(ctx, sqlPickRateKey, types).Scan(&key)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		l, err := scanLease(tx.QueryRow(ctx, sqlLease, types, workerID, lease, key))
		if err != nil || l == nil {
			return err // no task found: commit without moving next_free_at
		}
		if _, err := tx.Exec(ctx, sqlAdvanceRateKey, key); err != nil {
			return err
		}
		out = l
		return nil
	})
	if err != nil || out != nil {
		return out, err
	}
	return scanLease(s.pool.QueryRow(ctx, sqlLease, types, workerID, lease, nil))
}

func scanLease(row pgx.Row) (*Leased, error) {
	var l Leased
	err := row.Scan(&l.TaskID, &l.Type, &l.Key, &l.Payload, &l.Attempt, &l.RunID, &l.SnapshotID, &l.Seed)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &l, nil
}

// Heartbeat extends a lease. alive = false means the lease is lost and the
// worker must stop the task.
func (s *Store) Heartbeat(ctx context.Context, taskID string, attempt int64, lease time.Duration) (alive bool, err error) {
	tag, err := s.pool.Exec(ctx, sqlHeartbeat, taskID, attempt, lease)
	if err != nil {
		return false, err
	}
	if tag.RowsAffected() == 1 {
		return true, nil
	}
	// A stale heartbeat changes nothing, but we log it: it shows that a
	// zombie was told to stop. The log line is only information, so it does
	// not need to be in one transaction with the (empty) update.
	_, err = s.pool.Exec(ctx, sqlInsertEvent, taskID, attempt, "STALE_REJECTED", `{"rpc": "Heartbeat"}`)
	return false, err
}

// Complete marks a task SUCCEEDED and commits its output, its records and
// its sub-DAG in the same transaction. A zombie worker gets Stale and none
// of its data is written.
func (s *Store) Complete(ctx context.Context, taskID string, attempt int64, output []byte, records []Record, children []NewTask) (Outcome, error) {
	outcome := OK
	var completedType string
	var sinceLease time.Duration
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		outcome = OK
		var runID string
		err := tx.QueryRow(ctx, sqlComplete, taskID, attempt, jsonOrNil(output)).Scan(&runID)
		if errors.Is(err, pgx.ErrNoRows) {
			var state string
			var current int64
			err := tx.QueryRow(ctx, sqlTaskStateAttempt, taskID).Scan(&state, &current)
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			if err != nil {
				return err
			}
			if state == "SUCCEEDED" && current == attempt {
				return nil // rule 5: the same Complete arrived twice
			}
			outcome = Stale
			return insertEvent(ctx, tx, taskID, attempt, "STALE_REJECTED",
				map[string]any{"rpc": "Complete", "state": state, "current_attempt": current})
		}
		if err != nil {
			return err
		}

		if len(records) > 0 {
			kinds, keys, data := make([]string, len(records)), make([]string, len(records)), make([]string, len(records))
			for i, r := range records {
				kinds[i], keys[i], data[i] = r.Kind, r.Key, jsonOrEmptyObject(r.Data)
			}
			if _, err := tx.Exec(ctx, sqlInsertRecords, runID, taskID, attempt, kinds, keys, data); err != nil {
				var pgErr *pgconn.PgError
				if errors.As(err, &pgErr) && pgErr.Code == "23505" {
					return ErrDuplicateRecord
				}
				return err
			}
		}
		if err := insertDAG(ctx, tx, s.cfg, runID, children); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, sqlLockPendingChildren, []string{taskID}); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, sqlReleaseChildren, taskID); err != nil {
			return err
		}
		if err := insertEvent(ctx, tx, taskID, attempt, "SUCCEEDED", nil); err != nil {
			return err
		}
		completedType, sinceLease = "", 0
		if s.cfg.OnTaskDone != nil {
			var seconds float64
			if err := tx.QueryRow(ctx, sqlTaskDuration, taskID, attempt).Scan(&completedType, &seconds); err == nil {
				sinceLease = time.Duration(seconds * float64(time.Second))
			}
		}
		return nil
	})
	if err == nil && completedType != "" {
		s.cfg.OnTaskDone(completedType, sinceLease)
	}
	return outcome, err
}

// Fail records an explicit failure. backoff gets the number of earlier
// failures and returns how long to wait before the next try.
func (s *Store) Fail(ctx context.Context, taskID string, attempt int64, errMsg string, fatal bool, backoff func(failures int) time.Duration) (Outcome, error) {
	outcome := OK
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		outcome = OK
		var failures, maxAttempts int
		err := tx.QueryRow(ctx, sqlLockLeased, taskID, attempt).Scan(&failures, &maxAttempts)
		if errors.Is(err, pgx.ErrNoRows) {
			var already bool
			if err := tx.QueryRow(ctx, sqlHasEvent, taskID, attempt, "FAILED").Scan(&already); err != nil {
				return err
			}
			if already {
				return nil // rule 5: the same Fail arrived twice
			}
			outcome = Stale
			return insertEvent(ctx, tx, taskID, attempt, "STALE_REJECTED", map[string]any{"rpc": "Fail"})
		}
		if err != nil {
			return err
		}

		if fatal || failures+1 >= maxAttempts {
			if _, err := tx.Exec(ctx, sqlFailDead, taskID, attempt, errMsg); err != nil {
				return err
			}
			if err := insertEvent(ctx, tx, taskID, attempt, "FAILED", map[string]any{"error": errMsg, "fatal": fatal}); err != nil {
				return err
			}
			if err := insertEvent(ctx, tx, taskID, attempt, "DEAD_LETTER", nil); err != nil {
				return err
			}
			outcome = Dead
			return propagateFailure(ctx, tx, []string{taskID})
		}

		wait := backoff(failures)
		if _, err := tx.Exec(ctx, sqlFailRetry, taskID, attempt, errMsg, wait); err != nil {
			return err
		}
		return insertEvent(ctx, tx, taskID, attempt, "FAILED",
			map[string]any{"error": errMsg, "retry_in_ms": wait.Milliseconds()})
	})
	return outcome, err
}

type ReapResult struct {
	Expired      int
	DeadLettered int
	RunsClosed   int
}

// Reap returns expired leases to READY and closes finished runs. It is
// idempotent, so every coordinator can run it at the same time.
func (s *Store) Reap(ctx context.Context) (ReapResult, error) {
	var res ReapResult
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		res = ReapResult{}
		rows, err := tx.Query(ctx, sqlReap, s.cfg.MaxExpiries)
		if err != nil {
			return err
		}
		var ids, dead []string
		var attempts, deadAttempts []int32
		for rows.Next() {
			var id, state string
			var attempt int32
			if err := rows.Scan(&id, &attempt, &state); err != nil {
				rows.Close()
				return err
			}
			ids, attempts = append(ids, id), append(attempts, attempt)
			if state == "DEAD_LETTER" {
				dead, deadAttempts = append(dead, id), append(deadAttempts, attempt)
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		res.Expired, res.DeadLettered = len(ids), len(dead)
		if len(ids) > 0 {
			if _, err := tx.Exec(ctx, sqlInsertEvents, ids, attempts, "EXPIRED"); err != nil {
				return err
			}
		}
		if len(dead) > 0 {
			if _, err := tx.Exec(ctx, sqlInsertEvents, dead, deadAttempts, "DEAD_LETTER"); err != nil {
				return err
			}
			if err := propagateFailure(ctx, tx, dead); err != nil {
				return err
			}
		}
		tag, err := tx.Exec(ctx, sqlCloseRuns)
		if err != nil {
			return err
		}
		res.RunsClosed = int(tag.RowsAffected())
		return nil
	})
	return res, err
}

// propagateFailure tells the children of failed tasks. Children that
// tolerate failures move closer to READY; strict children are cancelled,
// and their own children are handled the same way.
func propagateFailure(ctx context.Context, tx pgx.Tx, failed []string) error {
	for len(failed) > 0 {
		if _, err := tx.Exec(ctx, sqlLockPendingChildren, failed); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, sqlReleaseTolerantChildren, failed); err != nil {
			return err
		}
		cancelled, err := queryIDAttempts(ctx, tx, sqlCancelStrictChildren, failed)
		if err != nil {
			return err
		}
		if err := logCancelled(ctx, tx, cancelled); err != nil {
			return err
		}
		failed = cancelled.ids
	}
	return nil
}

func cancelAndCascade(ctx context.Context, tx pgx.Tx, cancelled idAttempts) error {
	if err := logCancelled(ctx, tx, cancelled); err != nil {
		return err
	}
	return propagateFailure(ctx, tx, cancelled.ids)
}

func logCancelled(ctx context.Context, tx pgx.Tx, c idAttempts) error {
	if len(c.ids) == 0 {
		return nil
	}
	_, err := tx.Exec(ctx, sqlInsertEvents, c.ids, c.attempts, "CANCELLED")
	return err
}

// --- small reads --------------------------------------------------------------

// TaskRun returns the run a task belongs to.
func (s *Store) TaskRun(ctx context.Context, taskID string) (string, error) {
	var runID string
	err := s.pool.QueryRow(ctx, sqlTaskRun, taskID).Scan(&runID)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", ErrNotFound
	}
	return runID, err
}

// RunState returns RUNNING, SUCCEEDED or FAILED.
func (s *Store) RunState(ctx context.Context, runID string) (string, error) {
	var state string
	err := s.pool.QueryRow(ctx, sqlRunState, runID).Scan(&state)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", ErrNotFound
	}
	return state, err
}

type StateCount struct {
	Type  string
	State string
	Count int
}

func (s *Store) StateCounts(ctx context.Context, runID string) ([]StateCount, error) {
	rows, err := s.pool.Query(ctx, sqlStateCounts, runID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (StateCount, error) {
		var c StateCount
		err := row.Scan(&c.Type, &c.State, &c.Count)
		return c, err
	})
}

// ActiveCounts returns how many tasks are PENDING, READY and LEASED.
func (s *Store) ActiveCounts(ctx context.Context) (map[string]int, error) {
	rows, err := s.pool.Query(ctx, sqlActiveCounts)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{"PENDING": 0, "READY": 0, "LEASED": 0}
	for rows.Next() {
		var state string
		var n int
		if err := rows.Scan(&state, &n); err != nil {
			return nil, err
		}
		out[state] = n
	}
	return out, rows.Err()
}

// SetRateLimit sets the minimum gap between two leases for a rate key.
func (s *Store) SetRateLimit(ctx context.Context, key string, minInterval time.Duration) error {
	_, err := s.pool.Exec(ctx, sqlSetRateLimit, key, minInterval)
	return err
}

// --- helpers ------------------------------------------------------------------

type idAttempts struct {
	ids      []string
	attempts []int32
}

func queryIDAttempts(ctx context.Context, tx pgx.Tx, sql string, args ...any) (idAttempts, error) {
	var out idAttempts
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var attempt int32
		if err := rows.Scan(&id, &attempt); err != nil {
			return out, err
		}
		out.ids, out.attempts = append(out.ids, id), append(out.attempts, attempt)
	}
	return out, rows.Err()
}

func queryStrings(ctx context.Context, tx pgx.Tx, sql string, args ...any) ([]string, error) {
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

func insertEvent(ctx context.Context, tx pgx.Tx, taskID string, attempt int64, event string, detail map[string]any) error {
	var js *string
	if detail != nil {
		b, err := json.Marshal(detail)
		if err != nil {
			return err
		}
		str := string(b)
		js = &str
	}
	_, err := tx.Exec(ctx, sqlInsertEvent, taskID, attempt, event, js)
	return err
}

func jsonOrEmptyObject(b []byte) string {
	if len(b) == 0 {
		return "{}"
	}
	return string(b)
}

func jsonOrNil(b []byte) *string {
	if len(b) == 0 {
		return nil
	}
	s := string(b)
	return &s
}
