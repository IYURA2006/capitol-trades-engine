package engine_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"capitoltrades/coordinator/internal/engine"
	"capitoltrades/coordinator/internal/store"
	"capitoltrades/coordinator/internal/store/storetest"
)

func setup(t *testing.T) (context.Context, *engine.Engine, *store.Store, *pgxpool.Pool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	t.Cleanup(cancel)
	pool := storetest.NewPool(t)
	s := store.New(pool, store.Config{})
	return ctx, engine.New(s, engine.DefaultConfig()), s, pool
}

func count(t *testing.T, ctx context.Context, pool *pgxpool.Pool, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(ctx, sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func fanOut(n int) []engine.Spec {
	specs := make([]engine.Spec, 0, n+1)
	deps := make([]string, 0, n)
	for i := 0; i < n; i++ {
		key := fmt.Sprintf("%05d", i)
		specs = append(specs, engine.Spec{Type: "leaf", Key: key})
		deps = append(deps, engine.Ref("leaf", key))
	}
	return append(specs, engine.Spec{Type: "join", Key: "all", DependsOn: deps})
}

// drain runs workers until no task of the given types is left to lease.
func drain(t *testing.T, ctx context.Context, s *store.Store, workers int, types ...string) {
	t.Helper()
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for {
				l, err := s.Lease(ctx, fmt.Sprintf("w%d", w), types, time.Minute)
				if err != nil {
					t.Error(err)
					return
				}
				if l == nil {
					return
				}
				if _, err := s.Complete(ctx, l.TaskID, l.Attempt, nil, nil, nil); err != nil {
					t.Error(err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
}

// One task fans out to 10,000 children and a join. The join must run
// last, and exactly once.
func TestFanOutTenThousandThenJoin(t *testing.T) {
	ctx, eng, s, pool := setup(t)
	if _, err := eng.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "fan"}, []engine.Spec{{Type: "root", Key: "1"}}); err != nil {
		t.Fatal(err)
	}
	root, err := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	if err != nil || root == nil {
		t.Fatalf("lease root: %v %v", root, err)
	}

	start := time.Now()
	subDAG := fanOut(10_000)
	for i := 0; i < 2; i++ { // the second call is a retried RPC and must add nothing
		outcome, err := eng.Complete(ctx, root.TaskID, root.Attempt, nil, nil, subDAG)
		if err != nil || outcome != store.OK {
			t.Fatalf("Complete #%d with sub-DAG: %v %v", i+1, outcome, err)
		}
	}
	t.Logf("inserted 10,001 tasks and 10,000 dependencies in %v", time.Since(start).Round(time.Millisecond))

	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks`); n != 10_002 {
		t.Fatalf("tasks = %d, want 10002", n)
	}
	if n := count(t, ctx, pool, `SELECT pending_parents FROM tasks WHERE type = 'join'`); n != 10_000 {
		t.Fatalf("join pending_parents = %d, want 10000", n)
	}
	if l, _ := s.Lease(ctx, "w", []string{"join"}, time.Minute); l != nil {
		t.Fatal("join was leased before its parents ran")
	}

	start = time.Now()
	drain(t, ctx, s, 16, "leaf", "join")
	t.Logf("ran 10,001 tasks with 16 in-process workers in %v", time.Since(start).Round(time.Millisecond))

	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE state <> 'SUCCEEDED'`); n != 0 {
		t.Fatalf("%d tasks did not succeed", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id
		WHERE t.type = 'join' AND e.event = 'SUCCEEDED'`); n != 1 {
		t.Fatalf("join SUCCEEDED events = %d, want 1", n)
	}
	// The join's lease must be the last lease of all.
	if n := count(t, ctx, pool, `
		SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id
		WHERE t.type = 'leaf' AND e.event = 'SUCCEEDED'
		  AND e.seq > (SELECT min(j.seq) FROM task_events j JOIN tasks jt ON jt.id = j.task_id
		               WHERE jt.type = 'join' AND j.event = 'LEASED')`); n != 0 {
		t.Fatalf("%d leaves finished after the join was leased", n)
	}
}

// A bad sub-DAG must leave nothing behind: no children, and the parent
// still LEASED so the worker can report a failure.
func TestInvalidSubDAGInsertsNothing(t *testing.T) {
	ctx, eng, s, pool := setup(t)
	if _, err := eng.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "fan"}, []engine.Spec{{Type: "root", Key: "1"}}); err != nil {
		t.Fatal(err)
	}
	root, _ := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	bad := append(fanOut(100), engine.Spec{Type: "x", Key: "1", DependsOn: []string{"missing/1"}})

	_, err := eng.Complete(ctx, root.TaskID, root.Attempt, nil, nil, bad)
	if !errors.Is(err, engine.ErrInvalid) {
		t.Fatalf("err = %v, want ErrInvalid", err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks`); n != 1 {
		t.Fatalf("tasks = %d, want 1 (only the root)", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE state = 'LEASED'`); n != 1 {
		t.Fatal("root is no longer LEASED")
	}
}

// A duplicate record key makes the whole Complete fail: no records, no
// children, parent still LEASED. Either everything commits or nothing.
func TestCompleteIsAllOrNothing(t *testing.T) {
	ctx, eng, s, pool := setup(t)
	if _, err := eng.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "fan"},
		[]engine.Spec{{Type: "root", Key: "1"}, {Type: "root", Key: "2"}}); err != nil {
		t.Fatal(err)
	}
	first, _ := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	rec := []store.Record{{Kind: "k", Key: "same", Data: []byte(`{}`)}}
	if _, err := eng.Complete(ctx, first.TaskID, first.Attempt, nil, rec, nil); err != nil {
		t.Fatal(err)
	}
	second, _ := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	_, err := eng.Complete(ctx, second.TaskID, second.Attempt, nil, rec, fanOut(10))
	if !errors.Is(err, store.ErrDuplicateRecord) {
		t.Fatalf("err = %v, want ErrDuplicateRecord", err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks`); n != 2 {
		t.Fatalf("tasks = %d, want 2 (the sub-DAG must be rolled back)", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE state = 'LEASED'`); n != 1 {
		t.Fatal("the second root should still be LEASED")
	}
}

// Two parents emit the same child key. The second insert is a no-op, and a
// new task that depends on the already finished child must not wait forever.
func TestTwoParentsEmitTheSameChild(t *testing.T) {
	ctx, eng, s, pool := setup(t)
	if _, err := eng.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "fan"},
		[]engine.Spec{{Type: "root", Key: "1"}, {Type: "root", Key: "2"}}); err != nil {
		t.Fatal(err)
	}
	sub := func(own string) []engine.Spec {
		return []engine.Spec{
			{Type: "shared", Key: "s"},
			{Type: "own", Key: own, DependsOn: []string{"shared/s"}},
		}
	}
	first, _ := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	if _, err := eng.Complete(ctx, first.TaskID, first.Attempt, nil, nil, sub("a")); err != nil {
		t.Fatal(err)
	}
	drain(t, ctx, s, 1, "shared") // shared/s is now SUCCEEDED

	second, _ := s.Lease(ctx, "w", []string{"root"}, time.Minute)
	if _, err := eng.Complete(ctx, second.TaskID, second.Attempt, nil, nil, sub("b")); err != nil {
		t.Fatal(err)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE type = 'shared'`); n != 1 {
		t.Fatalf("shared tasks = %d, want 1", n)
	}
	if n := count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE type = 'own' AND state = 'READY' AND pending_parents = 0`); n != 2 {
		t.Fatalf("READY own tasks = %d, want 2 (own/b must not wait for a finished parent)", n)
	}
	drain(t, ctx, s, 2, "own")
	if _, err := s.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	if st, _ := s.RunState(ctx, "r1"); st != "SUCCEEDED" {
		t.Fatalf("run = %s, want SUCCEEDED", st)
	}
}

// 20 leasers share one rate key. Postgres must keep every two leases for
// that key at least min_interval apart (invariant 7).
func TestRateLimitIsSharedByAllLeasers(t *testing.T) {
	ctx, eng, s, pool := setup(t)
	const interval = 200 * time.Millisecond
	const tasks = 12
	if err := s.SetRateLimit(ctx, "house-clerk", interval); err != nil {
		t.Fatal(err)
	}
	specs := make([]engine.Spec, 0, tasks+5)
	for i := 0; i < tasks; i++ {
		specs = append(specs, engine.Spec{Type: "fetch", Key: fmt.Sprintf("%02d", i), RateKey: "house-clerk"})
	}
	for i := 0; i < 5; i++ { // tasks without a rate key must not be slowed down
		specs = append(specs, engine.Spec{Type: "free", Key: fmt.Sprintf("%02d", i)})
	}
	if _, err := eng.SubmitRun(ctx, store.Run{ID: "r1", Workflow: "rate"}, specs); err != nil {
		t.Fatal(err)
	}

	start := time.Now()
	var wg sync.WaitGroup
	for w := 0; w < 20; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for count(t, ctx, pool, `SELECT count(*) FROM tasks WHERE state IN ('READY', 'LEASED')`) > 0 {
				l, err := s.Lease(ctx, fmt.Sprintf("w%d", w), []string{"fetch", "free"}, time.Minute)
				if err != nil {
					t.Error(err)
					return
				}
				if l == nil {
					time.Sleep(5 * time.Millisecond) // a worker polls; it does not spin
					continue
				}
				if _, err := s.Complete(ctx, l.TaskID, l.Attempt, nil, nil, nil); err != nil {
					t.Error(err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
	elapsed := time.Since(start)

	var leases int
	var minGapMs float64
	err := pool.QueryRow(ctx, `
		SELECT count(*), coalesce(min(gap), 0) FROM (
		  SELECT extract(epoch FROM e.at - lag(e.at) OVER (ORDER BY e.at)) * 1000 AS gap
		  FROM task_events e JOIN tasks t ON t.id = e.task_id
		  WHERE t.rate_key = 'house-clerk' AND e.event = 'LEASED') g`).Scan(&leases, &minGapMs)
	if err != nil {
		t.Fatal(err)
	}
	if leases != tasks {
		t.Fatalf("rate-limited leases = %d, want %d", leases, tasks)
	}
	if minGapMs < float64(interval.Milliseconds()) {
		t.Fatalf("smallest gap between leases = %.1f ms, want at least %d ms", minGapMs, interval.Milliseconds())
	}
	if want := time.Duration(tasks-1) * interval; elapsed < want {
		t.Fatalf("finished in %v; %d rate-limited tasks need at least %v", elapsed, tasks, want)
	}
	t.Logf("%d leases, smallest gap %.1f ms, total %v", leases, minGapMs, elapsed.Round(time.Millisecond))
}
