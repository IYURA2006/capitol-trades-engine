package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// A checker that never fails proves nothing. These tests build one small,
// correct run by hand, then break it in one way at a time and expect the
// matching invariant to fire. Everything happens in a transaction that is
// rolled back, so the database is left untouched.
//
// Needs the Compose Postgres with the engine schema (`make up`).

const goodRun = `
INSERT INTO runs (id, workflow, seed, state) VALUES ('chk', 'test', 1, 'SUCCEEDED');
INSERT INTO rate_limits (key, min_interval) VALUES ('chk-key', '200 ms');
INSERT INTO tasks (id, run_id, type, key, payload, state, attempt, rate_key) VALUES
  ('chk-a', 'chk', 'a', '1', '{}', 'SUCCEEDED', 1, 'chk-key'),
  ('chk-b', 'chk', 'b', '1', '{}', 'SUCCEEDED', 2, 'chk-key'),
  ('chk-c', 'chk', 'c', '1', '{}', 'SUCCEEDED', 1, NULL);
INSERT INTO task_deps (child_id, parent_id) VALUES ('chk-c', 'chk-a'), ('chk-c', 'chk-b');
INSERT INTO task_events (task_id, attempt, event, at) VALUES
  ('chk-a', 1, 'LEASED',    '2026-01-01 00:00:00.000+00'),
  ('chk-b', 1, 'LEASED',    '2026-01-01 00:00:00.300+00'),
  ('chk-a', 1, 'SUCCEEDED', '2026-01-01 00:00:00.400+00'),
  ('chk-b', 1, 'EXPIRED',   '2026-01-01 00:00:06.000+00'),
  ('chk-b', 2, 'LEASED',    '2026-01-01 00:00:06.100+00'),
  ('chk-b', 2, 'SUCCEEDED', '2026-01-01 00:00:06.200+00'),
  ('chk-c', 1, 'LEASED',    '2026-01-01 00:00:06.300+00'),
  ('chk-c', 1, 'SUCCEEDED', '2026-01-01 00:00:06.400+00');
INSERT INTO task_events (task_id, attempt, event, detail) VALUES
  ('chk-b', 1, 'STALE_REJECTED', '{"rpc": "Complete"}');
INSERT INTO records (run_id, kind, key, data, task_id, attempt) VALUES
  ('chk', 'out', 'a', '{"v": 1, "f": 0.5}', 'chk-a', 1),
  ('chk', 'out', 'b', '{"v": 2}',           'chk-b', 2),
  ('chk', 'out', 'c', '{"v": 3}',           'chk-c', 1);
`

var goodReference = &Reference{
	RunState: "SUCCEEDED",
	Tasks:    map[string]string{"a/1": "SUCCEEDED", "b/1": "SUCCEEDED", "c/1": "SUCCEEDED"},
	// Key order differs from the database on purpose: jsonb ignores it.
	Records: json.RawMessage(`[
		{"kind": "out", "key": "a", "data": {"f": 0.5, "v": 1}},
		{"kind": "out", "key": "b", "data": {"v": 2}},
		{"kind": "out", "key": "c", "data": {"v": 3}}]`),
}

func TestCheckerCatchesEveryKindOfDamage(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		url = "postgres://engine:engine@localhost:5433/engine?sslmode=disable"
	}
	conn, err := pgx.Connect(ctx, url)
	if err != nil {
		t.Fatalf("connect (run `make up` first): %v", err)
	}
	defer conn.Close(ctx)
	outer, err := conn.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = outer.Rollback(ctx) }()
	if _, err := outer.Exec(ctx, goodRun); err != nil {
		t.Fatalf("build the good run: %v", err)
	}
	tasksJSON, _ := json.Marshal(goodReference.Tasks)

	cases := []struct {
		name   string
		damage string // SQL that breaks the run; empty = the untouched run
		want   string // the invariant that must report it; empty = no violation
	}{
		{"the correct run passes", ``, ``},
		{"a task is still leased",
			`UPDATE tasks SET state = 'LEASED' WHERE id = 'chk-c'`, "1: every task"},
		{"the run never ended",
			`UPDATE runs SET state = 'RUNNING' WHERE id = 'chk'`, "1: the run"},
		{"a child was leased before its parent finished",
			`INSERT INTO task_events (seq, task_id, attempt, event) VALUES (-5, 'chk-c', 0, 'LEASED')`, "2: a task is first leased"},
		{"a strict child ran although a parent dead-lettered",
			`UPDATE tasks SET state = 'DEAD_LETTER' WHERE id = 'chk-a'`, "2: a strict task"},
		{"a task succeeded twice",
			`INSERT INTO task_events (task_id, attempt, event) VALUES ('chk-b', 1, 'SUCCEEDED')`, "3: no task has more than one"},
		{"a record was written by a zombie attempt",
			`UPDATE records SET attempt = 1 WHERE key = 'b'`, "4: every record"},
		{"a task is missing",
			`DELETE FROM task_deps WHERE child_id = 'chk-c'; DELETE FROM records WHERE task_id = 'chk-c';
			 DELETE FROM tasks WHERE id = 'chk-c'`, "5: task set"},
		{"an extra task exists",
			`INSERT INTO tasks (id, run_id, type, key, payload, state) VALUES ('chk-x', 'chk', 'x', '1', '{}', 'CANCELLED')`, "5: task set"},
		{"a task ended in another state",
			`UPDATE tasks SET state = 'CANCELLED' WHERE id = 'chk-c'; DELETE FROM records WHERE task_id = 'chk-c'`, "5: task set"},
		{"a record has a different value",
			`UPDATE records SET data = '{"v": 2.0000000001}' WHERE key = 'b'`, "6: records equal"},
		// Equal as a number, but printed differently. The SDK always prints a
		// number the same way, so a difference here means two code paths
		// disagree, and the strict hash reports it.
		{"a number is printed in another format",
			`UPDATE records SET data = '{"v": 1, "f": 0.50}' WHERE key = 'a'`, "6: results hash"},
		{"a record is missing",
			`DELETE FROM records WHERE key = 'c'`, "6: records equal"},
		{"an extra record exists",
			`INSERT INTO records (run_id, kind, key, data, task_id, attempt) VALUES ('chk', 'out', 'zz', '{}', 'chk-c', 1)`, "6: records equal"},
		{"two rate-limited leases are too close",
			`UPDATE task_events SET at = '2026-01-01 00:00:00.150+00' WHERE task_id = 'chk-b' AND attempt = 1 AND event = 'LEASED'`, "7: leases"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			tx, err := outer.Begin(ctx) // a savepoint; rolled back after the case
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = tx.Rollback(ctx) }()
			if c.damage != "" {
				if _, err := tx.Exec(ctx, c.damage); err != nil {
					t.Fatalf("damage: %v", err)
				}
			}
			res, err := checkTx(ctx, tx, "chk", goodReference, string(tasksJSON))
			if err != nil {
				t.Fatal(err)
			}
			if c.want == "" {
				if len(res.Violations) != 0 {
					t.Fatalf("violations on a correct run: %v", res.Violations)
				}
				if res.Hash != res.RefHash || res.Records != 3 || res.StaleComplete != 1 || res.Tasks != 3 {
					t.Fatalf("unexpected summary: %+v", res)
				}
				return
			}
			for _, v := range res.Violations {
				if strings.HasPrefix(v, c.want) {
					return
				}
			}
			t.Fatalf("want a violation starting with %q, got %v", c.want, res.Violations)
		})
	}
}
