package store_test

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"capitoltrades/coordinator/internal/engine"
	"capitoltrades/coordinator/internal/store"
	"capitoltrades/coordinator/internal/store/storetest"
)

const lease = 30 * time.Second

func noBackoff(int) time.Duration { return 0 }

func setup(t *testing.T) (context.Context, *store.Store, *pgxpool.Pool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	t.Cleanup(cancel)
	pool := storetest.NewPool(t)
	return ctx, store.New(pool, store.Config{MaxExpiries: 3}), pool
}

// submit creates run "r1" from specs and fails the test on any error.
func submit(t *testing.T, ctx context.Context, s *store.Store, specs ...engine.Spec) {
	t.Helper()
	tasks, err := engine.BuildDAG("r1", specs)
	if err != nil {
		t.Fatal(err)
	}
	created, err := s.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "test", Seed: 1}, tasks)
	if err != nil || !created {
		t.Fatalf("SubmitRun: created=%v err=%v", created, err)
	}
}

func mustLease(t *testing.T, ctx context.Context, s *store.Store, types ...string) *store.Leased {
	t.Helper()
	l, err := s.Lease(ctx, "w1", types, lease)
	if err != nil {
		t.Fatal(err)
	}
	if l == nil {
		t.Fatalf("expected a task of type %v, got none", types)
	}
	return l
}

func count(t *testing.T, ctx context.Context, pool *pgxpool.Pool, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(ctx, sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func taskState(t *testing.T, ctx context.Context, pool *pgxpool.Pool, taskType, key string) string {
	t.Helper()
	var state string
	if err := pool.QueryRow(ctx, `SELECT state FROM tasks WHERE type = $1 AND key = $2`, taskType, key).Scan(&state); err != nil {
		t.Fatal(err)
	}
	return state
}

func TestSubmitRunTwiceChangesNothing(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1"})
	tasks, _ := engine.BuildDAG("r1", []engine.Spec{{Type: "a", Key: "1"}, {Type: "a", Key: "2"}})
	created, err := s.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "test"}, tasks)
	if err != nil || created {
		t.Fatalf("second SubmitRun: created=%v err=%v, want false and nil", created, err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks`); n != 1 {
		t.Fatalf("tasks = %d, want 1", n)
	}
}

// 50 leasers compete for 1,000 tasks. SKIP LOCKED must give every task to
// exactly one of them.
func TestConcurrentLeasersGetEachTaskOnce(t *testing.T) {
	ctx, s, pool := setup(t)
	const tasks, leasers = 1000, 50
	specs := make([]engine.Spec, tasks)
	for i := range specs {
		specs[i] = engine.Spec{Type: "a", Key: fmt.Sprintf("%04d", i)}
	}
	submit(t, ctx, s, specs...)

	var mu sync.Mutex
	seen := map[string]int{}
	var wg sync.WaitGroup
	for w := 0; w < leasers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for {
				l, err := s.Lease(ctx, fmt.Sprintf("w%d", w), []string{"a"}, lease)
				if err != nil {
					t.Error(err)
					return
				}
				if l == nil {
					return
				}
				mu.Lock()
				seen[l.TaskID]++
				mu.Unlock()
			}
		}(w)
	}
	wg.Wait()

	if len(seen) != tasks {
		t.Fatalf("leased %d different tasks, want %d", len(seen), tasks)
	}
	for id, n := range seen {
		if n != 1 {
			t.Fatalf("task %s was leased %d times", id, n)
		}
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM (
		SELECT task_id, attempt FROM task_events WHERE event = 'LEASED'
		GROUP BY task_id, attempt HAVING count(*) > 1) d`); n != 0 {
		t.Fatalf("%d (task, attempt) pairs have more than one LEASED event", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE state = 'LEASED' AND attempt = 1`); n != tasks {
		t.Fatalf("LEASED tasks with attempt 1 = %d, want %d", n, tasks)
	}
}

// A zombie worker (old attempt) must not be able to finish the task or
// write any data.
func TestCompleteWithOldAttemptIsRejected(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1"})

	old, err := s.Lease(ctx, "zombie", []string{"a"}, 50*time.Millisecond)
	if err != nil || old == nil {
		t.Fatalf("first lease: %v %v", old, err)
	}
	time.Sleep(120 * time.Millisecond) // real time must pass for the lease to expire
	if _, err := s.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	fresh := mustLease(t, ctx, s, "a")
	if fresh.Attempt != old.Attempt+1 {
		t.Fatalf("new attempt = %d, want %d", fresh.Attempt, old.Attempt+1)
	}

	outcome, err := s.Complete(ctx, old.TaskID, old.Attempt, []byte(`{"from":"zombie"}`),
		[]store.Record{{Kind: "k", Key: "x", Data: []byte(`{"v":1}`)}}, nil)
	if err != nil || outcome != store.Stale {
		t.Fatalf("zombie Complete: outcome=%v err=%v, want Stale", outcome, err)
	}
	if st := taskState(t, ctx, pool, "a", "1"); st != "LEASED" {
		t.Fatalf("state = %s, want LEASED (still owned by the new worker)", st)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM records`); n != 0 {
		t.Fatalf("records = %d, want 0", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM task_events WHERE event = 'STALE_REJECTED'`); n != 1 {
		t.Fatalf("STALE_REJECTED events = %d, want 1", n)
	}

	// The real owner can still finish.
	outcome, err = s.Complete(ctx, fresh.TaskID, fresh.Attempt, nil,
		[]store.Record{{Kind: "k", Key: "x", Data: []byte(`{"v":2}`)}}, nil)
	if err != nil || outcome != store.OK {
		t.Fatalf("owner Complete: outcome=%v err=%v", outcome, err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM records WHERE attempt = $1`, fresh.Attempt); n != 1 {
		t.Fatalf("records from the owner = %d, want 1", n)
	}
}

// A retried RPC (same attempt) must succeed twice and write once.
func TestCompleteTwiceWritesOnce(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s,
		engine.Spec{Type: "a", Key: "1"},
		engine.Spec{Type: "b", Key: "1", DependsOn: []string{"a/1"}})
	l := mustLease(t, ctx, s, "a")
	records := []store.Record{{Kind: "k", Key: "x", Data: []byte(`{"v":1}`)}, {Kind: "k", Key: "y", Data: []byte(`{"v":2}`)}}

	for i := 0; i < 2; i++ {
		outcome, err := s.Complete(ctx, l.TaskID, l.Attempt, []byte(`{"ok":true}`), records, nil)
		if err != nil || outcome != store.OK {
			t.Fatalf("Complete #%d: outcome=%v err=%v", i+1, outcome, err)
		}
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM records`); n != 2 {
		t.Fatalf("records = %d, want 2", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM task_events WHERE event = 'SUCCEEDED'`); n != 1 {
		t.Fatalf("SUCCEEDED events = %d, want 1", n)
	}
	if n := count(t, ctx, pool, `SELECT pending_parents FROM tasks WHERE type = 'b'`); n != 0 {
		t.Fatalf("child pending_parents = %d, want 0 (decremented once, not twice)", n)
	}
	if st := taskState(t, ctx, pool, "b", "1"); st != "READY" {
		t.Fatalf("child state = %s, want READY", st)
	}
}

func TestExpiredLeaseIsLeasedAgainWithNextAttempt(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1"})

	first, err := s.Lease(ctx, "w1", []string{"a"}, 50*time.Millisecond)
	if err != nil || first == nil {
		t.Fatalf("first lease: %v %v", first, err)
	}
	if l, _ := s.Lease(ctx, "w2", []string{"a"}, lease); l != nil {
		t.Fatal("task was leased twice before the lease expired")
	}
	time.Sleep(120 * time.Millisecond)
	res, err := s.Reap(ctx)
	if err != nil || res.Expired != 1 {
		t.Fatalf("Reap: %+v err=%v, want 1 expired", res, err)
	}
	second := mustLease(t, ctx, s, "a")
	if second.Attempt != first.Attempt+1 {
		t.Fatalf("attempt = %d, want %d", second.Attempt, first.Attempt+1)
	}
	if n := count(t, ctx, pool, `SELECT expiries * 10 + failures FROM tasks`); n != 10 {
		t.Fatalf("expiries*10+failures = %d, want 10 (one expiry, no failure)", n)
	}
	// The first worker's heartbeat must now report a lost lease.
	alive, err := s.Heartbeat(ctx, first.TaskID, first.Attempt, lease)
	if err != nil || alive {
		t.Fatalf("old heartbeat: alive=%v err=%v, want false", alive, err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM task_events
		WHERE event = 'STALE_REJECTED' AND attempt = $1 AND detail->>'rpc' = 'Heartbeat'`, first.Attempt); n != 1 {
		t.Fatalf("STALE_REJECTED heartbeat events = %d, want 1", n)
	}
	alive, err = s.Heartbeat(ctx, second.TaskID, second.Attempt, lease)
	if err != nil || !alive {
		t.Fatalf("new heartbeat: alive=%v err=%v, want true", alive, err)
	}
}

func TestTooManyExpiriesDeadLetters(t *testing.T) {
	ctx, s, pool := setup(t) // MaxExpiries = 3
	submit(t, ctx, s,
		engine.Spec{Type: "a", Key: "1"},
		engine.Spec{Type: "b", Key: "1", DependsOn: []string{"a/1"}})
	for i := 0; i < 3; i++ {
		if l, err := s.Lease(ctx, "w1", []string{"a"}, 20*time.Millisecond); err != nil || l == nil {
			t.Fatalf("lease %d: %v %v", i, l, err)
		}
		time.Sleep(60 * time.Millisecond)
		if _, err := s.Reap(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if st := taskState(t, ctx, pool, "a", "1"); st != "DEAD_LETTER" {
		t.Fatalf("state = %s, want DEAD_LETTER", st)
	}
	if st := taskState(t, ctx, pool, "b", "1"); st != "CANCELLED" {
		t.Fatalf("child state = %s, want CANCELLED", st)
	}
}

func TestFailRetriesThenDeadLetters(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1", MaxAttempts: 2})

	l := mustLease(t, ctx, s, "a")
	for i := 0; i < 2; i++ { // the second call is a retried RPC
		outcome, err := s.Fail(ctx, l.TaskID, l.Attempt, "boom", false, noBackoff)
		if err != nil || outcome != store.OK {
			t.Fatalf("Fail #%d: outcome=%v err=%v", i+1, outcome, err)
		}
	}
	if n := count(t, ctx, pool, `SELECT failures FROM tasks`); n != 1 {
		t.Fatalf("failures = %d, want 1 (a retried Fail must count once)", n)
	}
	if st := taskState(t, ctx, pool, "a", "1"); st != "READY" {
		t.Fatalf("state = %s, want READY", st)
	}

	l = mustLease(t, ctx, s, "a")
	if _, err := s.Fail(ctx, l.TaskID, l.Attempt, "boom again", false, noBackoff); err != nil {
		t.Fatal(err)
	}
	if st := taskState(t, ctx, pool, "a", "1"); st != "DEAD_LETTER" {
		t.Fatalf("state = %s, want DEAD_LETTER after max_attempts failures", st)
	}
}

func TestFailBackoffDelaysNextLease(t *testing.T) {
	ctx, s, _ := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1"})
	l := mustLease(t, ctx, s, "a")
	if _, err := s.Fail(ctx, l.TaskID, l.Attempt, "boom", false, func(int) time.Duration { return time.Hour }); err != nil {
		t.Fatal(err)
	}
	if again, _ := s.Lease(ctx, "w1", []string{"a"}, lease); again != nil {
		t.Fatal("task was leased before its backoff ended")
	}
}

// One parent dead-letters. The strict child and its child are cancelled;
// the tolerant join still runs.
func TestDeadLetterCancelsStrictChildrenAndKeepsTolerantJoin(t *testing.T) {
	ctx, s, pool := setup(t)
	submit(t, ctx, s,
		engine.Spec{Type: "parse", Key: "good"},
		engine.Spec{Type: "parse", Key: "bad"},
		engine.Spec{Type: "strict", Key: "1", DependsOn: []string{"parse/bad"}},
		engine.Spec{Type: "strict", Key: "2", DependsOn: []string{"strict/1"}},
		engine.Spec{Type: "join", Key: "all", DependsOn: []string{"parse/good", "parse/bad"}, TolerateFailures: true})

	for i := 0; i < 2; i++ {
		l := mustLease(t, ctx, s, "parse")
		if l.Key == "bad" {
			if _, err := s.Fail(ctx, l.TaskID, l.Attempt, "scanned", true, noBackoff); err != nil {
				t.Fatal(err)
			}
		} else if _, err := s.Complete(ctx, l.TaskID, l.Attempt, nil, nil, nil); err != nil {
			t.Fatal(err)
		}
	}

	for key, want := range map[string]string{"1": "CANCELLED", "2": "CANCELLED"} {
		if st := taskState(t, ctx, pool, "strict", key); st != want {
			t.Fatalf("strict/%s = %s, want %s", key, st, want)
		}
	}
	if st := taskState(t, ctx, pool, "join", "all"); st != "READY" {
		t.Fatalf("join = %s, want READY", st)
	}
	j := mustLease(t, ctx, s, "join")
	if _, err := s.Complete(ctx, j.TaskID, j.Attempt, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if res, err := s.Reap(ctx); err != nil || res.RunsClosed != 1 {
		t.Fatalf("Reap: %+v err=%v, want 1 run closed", res, err)
	}
	if st, _ := s.RunState(ctx, "r1"); st != "FAILED" {
		t.Fatalf("run = %s, want FAILED (tasks were cancelled)", st)
	}
}

func TestRunSucceedsWhenAllTasksSucceed(t *testing.T) {
	ctx, s, _ := setup(t)
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1"})
	if res, _ := s.Reap(ctx); res.RunsClosed != 0 {
		t.Fatal("run was closed while a task was still READY")
	}
	l := mustLease(t, ctx, s, "a")
	if _, err := s.Complete(ctx, l.TaskID, l.Attempt, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	if st, _ := s.RunState(ctx, "r1"); st != "SUCCEEDED" {
		t.Fatalf("run = %s, want SUCCEEDED", st)
	}
}

// Losing a lease is not the task's fault. Six expiries with max_attempts = 5
// must not dead-letter the task.
func TestSixExpiriesDoNotCountAsFailures(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool := storetest.NewPool(t)
	s := store.New(pool, store.Config{MaxExpiries: 20})
	submit(t, ctx, s, engine.Spec{Type: "a", Key: "1", MaxAttempts: 5})

	for i := 0; i < 6; i++ {
		if l, err := s.Lease(ctx, "doomed", []string{"a"}, 20*time.Millisecond); err != nil || l == nil {
			t.Fatalf("lease %d: %v %v", i+1, l, err)
		}
		time.Sleep(60 * time.Millisecond)
		if res, err := s.Reap(ctx); err != nil || res.Expired != 1 {
			t.Fatalf("reap %d: %+v %v", i+1, res, err)
		}
	}
	l := mustLease(t, ctx, s, "a")
	if l.Attempt != 7 {
		t.Fatalf("attempt = %d, want 7", l.Attempt)
	}
	if outcome, err := s.Complete(ctx, l.TaskID, l.Attempt, nil, nil, nil); err != nil || outcome != store.OK {
		t.Fatalf("Complete: %v %v", outcome, err)
	}
	if n := count(t, ctx, pool, `SELECT expiries * 100 + failures FROM tasks WHERE state = 'SUCCEEDED'`); n != 600 {
		t.Fatalf("expiries*100+failures = %d, want 600", n)
	}
}
