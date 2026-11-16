package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Reference is the output of `capitol_sdk.cli local`: what one process
// computes for the same workflow, snapshot and seed.
type Reference struct {
	RunState string            `json:"run_state"`
	Tasks    map[string]string `json:"tasks"` // "type/key" -> final state
	Records  json.RawMessage   `json:"records"`
}

func loadReference(path string) (*Reference, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var ref Reference
	if err := json.Unmarshal(raw, &ref); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &ref, nil
}

// CheckResult is what the invariant checker found for one run.
type CheckResult struct {
	Violations []string `json:"violations"` // empty = all seven invariants hold
	Tasks      int      `json:"tasks"`
	Records    int      `json:"records"`
	Hash       string   `json:"hash"`
	RefHash    string   `json:"ref_hash"`
	Expiries   int      `json:"expiries"`
	// Requests refused because the fencing token was old, by RPC.
	StaleComplete  int `json:"stale_complete"`
	StaleHeartbeat int `json:"stale_heartbeat"`
	StaleFail      int `json:"stale_fail"`
}

func (c *CheckResult) Stale() int { return c.StaleComplete + c.StaleHeartbeat + c.StaleFail }

// Each query returns the number of rows that break one invariant.
// They read only the final tables and the append-only event log.
var invariantQueries = []struct {
	name string
	sql  string
}{
	{"1: every task is in a terminal state", `
		SELECT count(*) FROM tasks
		WHERE run_id = $1 AND state NOT IN ('SUCCEEDED', 'DEAD_LETTER', 'CANCELLED')`},

	{"1: the run is in a terminal state", `
		SELECT count(*) FROM runs WHERE id = $1 AND state = 'RUNNING'`},

	// seq is the order of the event log, so no clock is involved.
	{"2: a task is first leased only after each parent has ended", `
		SELECT count(*) FROM task_deps d
		JOIN tasks c ON c.id = d.child_id
		WHERE c.run_id = $1
		  AND EXISTS (SELECT 1 FROM task_events WHERE task_id = c.id AND event = 'LEASED')
		  AND (SELECT min(seq) FROM task_events WHERE task_id = c.id AND event = 'LEASED')
		      < coalesce((SELECT min(seq) FROM task_events
		                  WHERE task_id = d.parent_id AND event IN ('SUCCEEDED', 'DEAD_LETTER', 'CANCELLED')),
		                 9223372036854775807)`},

	{"2: a strict task ran only if every parent succeeded", `
		SELECT count(*) FROM task_deps d
		JOIN tasks c ON c.id = d.child_id
		JOIN tasks p ON p.id = d.parent_id
		WHERE c.run_id = $1 AND NOT c.tolerate_failures AND p.state <> 'SUCCEEDED'
		  AND EXISTS (SELECT 1 FROM task_events WHERE task_id = c.id AND event = 'LEASED')`},

	{"3: no task has more than one SUCCEEDED event", `
		SELECT count(*) FROM (
		  SELECT e.task_id FROM task_events e JOIN tasks t ON t.id = e.task_id
		  WHERE t.run_id = $1 AND e.event = 'SUCCEEDED'
		  GROUP BY e.task_id HAVING count(*) > 1) x`},

	{"3: every SUCCEEDED task has exactly one SUCCEEDED event", `
		SELECT count(*) FROM tasks t
		WHERE t.run_id = $1 AND t.state = 'SUCCEEDED'
		  AND (SELECT count(*) FROM task_events WHERE task_id = t.id AND event = 'SUCCEEDED') <> 1`},

	{"4: every record comes from its task's final attempt", `
		SELECT count(*) FROM records r JOIN tasks t ON t.id = r.task_id
		WHERE r.run_id = $1 AND (r.attempt <> t.attempt OR t.state <> 'SUCCEEDED')`},

	{"5: task set and final states equal the reference", `
		SELECT count(*) FROM
		  (SELECT type || '/' || key AS ref, state FROM tasks WHERE run_id = $1) a
		  FULL JOIN ref_tasks b USING (ref)
		WHERE a.state IS DISTINCT FROM b.state`},

	// jsonb equality ignores key order and number formatting.
	{"6: records equal the reference", `
		SELECT count(*) FROM
		  (SELECT kind, key, data FROM records WHERE run_id = $1) a
		  FULL JOIN ref_records b USING (kind, key)
		WHERE a.data IS DISTINCT FROM b.data`},

	{"7: leases of one rate key are at least min_interval apart", `
		SELECT count(*) FROM (
		  SELECT e.at - lag(e.at) OVER (PARTITION BY t.rate_key ORDER BY e.at) AS gap, rl.min_interval
		  FROM task_events e
		  JOIN tasks t ON t.id = e.task_id
		  JOIN rate_limits rl ON rl.key = t.rate_key
		  WHERE t.run_id = $1 AND e.event = 'LEASED') g
		WHERE gap < min_interval`},
}

const sqlRecordsHash = `
SELECT count(*), coalesce(encode(sha256(convert_to(
         string_agg(kind || E'\n' || key || E'\n' || data::text, E'\n'
                    ORDER BY kind COLLATE "C", key COLLATE "C"), 'UTF8')), 'hex'), '')
FROM %s`

// check verifies the seven invariants for one finished run.
func check(ctx context.Context, pool *pgxpool.Pool, runID string, ref *Reference) (*CheckResult, error) {
	var res *CheckResult
	tasksJSON, err := json.Marshal(ref.Tasks)
	if err != nil {
		return nil, err
	}

	// One connection, one transaction: the temp tables live only in it.
	err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		var err error
		res, err = checkTx(ctx, tx, runID, ref, string(tasksJSON))
		return err
	})
	return res, err
}

// checkTx does the work of check inside an open transaction. Tests use it
// to break a run on purpose and roll the damage back afterwards.
func checkTx(ctx context.Context, tx pgx.Tx, runID string, ref *Reference, tasksJSON string) (*CheckResult, error) {
	res := &CheckResult{}
	err := func() error {
		setup := []struct {
			sql  string
			args []any
		}{
			{`CREATE TEMP TABLE ref_tasks (ref text PRIMARY KEY, state text) ON COMMIT DROP`, nil},
			{`CREATE TEMP TABLE ref_records (kind text, key text, data jsonb, PRIMARY KEY (kind, key)) ON COMMIT DROP`, nil},
			{`INSERT INTO ref_tasks SELECT key, value FROM jsonb_each_text($1::jsonb)`, []any{tasksJSON}},
			{`INSERT INTO ref_records SELECT kind, key, data
			  FROM jsonb_to_recordset($1::jsonb) AS r(kind text, key text, data jsonb)`, []any{string(ref.Records)}},
		}
		for _, s := range setup {
			if _, err := tx.Exec(ctx, s.sql, s.args...); err != nil {
				return err
			}
		}

		for _, q := range invariantQueries {
			var bad int
			if err := tx.QueryRow(ctx, q.sql, runID).Scan(&bad); err != nil {
				return fmt.Errorf("invariant %q: %w", q.name, err)
			}
			if bad > 0 {
				res.Violations = append(res.Violations, fmt.Sprintf("%s (%d rows)", q.name, bad))
			}
		}

		var runState string
		if err := tx.QueryRow(ctx, `SELECT state FROM runs WHERE id = $1`, runID).Scan(&runState); err != nil {
			return err
		}
		if runState != ref.RunState {
			res.Violations = append(res.Violations,
				fmt.Sprintf("5: run state is %s, reference is %s", runState, ref.RunState))
		}

		mine := fmt.Sprintf("(SELECT kind, key, data FROM records WHERE run_id = '%s') r", runID)
		if err := tx.QueryRow(ctx, fmt.Sprintf(sqlRecordsHash, mine)).Scan(&res.Records, &res.Hash); err != nil {
			return err
		}
		var refCount int
		if err := tx.QueryRow(ctx, fmt.Sprintf(sqlRecordsHash, "ref_records")).Scan(&refCount, &res.RefHash); err != nil {
			return err
		}
		if res.Hash != res.RefHash {
			res.Violations = append(res.Violations, "6: results hash differs from the reference")
		}

		if err := tx.QueryRow(ctx, `SELECT count(*), coalesce(sum(expiries), 0) FROM tasks WHERE run_id = $1`,
			runID).Scan(&res.Tasks, &res.Expiries); err != nil {
			return err
		}
		return tx.QueryRow(ctx, `
			SELECT count(*) FILTER (WHERE e.detail->>'rpc' = 'Complete'),
			       count(*) FILTER (WHERE e.detail->>'rpc' = 'Heartbeat'),
			       count(*) FILTER (WHERE e.detail->>'rpc' = 'Fail')
			FROM task_events e JOIN tasks t ON t.id = e.task_id
			WHERE t.run_id = $1 AND e.event = 'STALE_REJECTED'`, runID).
			Scan(&res.StaleComplete, &res.StaleHeartbeat, &res.StaleFail)
	}()
	return res, err
}
