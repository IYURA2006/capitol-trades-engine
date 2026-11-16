package server_test

import (
	"context"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"capitoltrades/coordinator/internal/engine"
	"capitoltrades/coordinator/internal/pb"
	"capitoltrades/coordinator/internal/server"
	"capitoltrades/coordinator/internal/store"
	"capitoltrades/coordinator/internal/store/storetest"
)

// startCoordinator runs a real gRPC server on a free local port, with its
// reaper, on top of a fresh database.
func startCoordinator(t *testing.T) (pb.EngineClient, *pgxpool.Pool) {
	t.Helper()
	pool := storetest.NewPool(t)
	cfg := engine.DefaultConfig()
	cfg.LongPoll = 500 * time.Millisecond
	cfg.PollInterval = 20 * time.Millisecond
	cfg.ReaperInterval = 100 * time.Millisecond
	eng := engine.New(store.New(pool, store.Config{}), cfg)

	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	pb.RegisterEngineServer(srv, server.New(eng))
	go func() { _ = srv.Serve(lis) }()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { eng.RunReaper(ctx); close(done) }()

	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Stop()
		cancel()
		<-done
	})
	return pb.NewEngineClient(conn), pool
}

// testWorker is the Go worker used only by tests: it leases a task, writes
// one record and completes it, until the run is no longer RUNNING.
func testWorker(ctx context.Context, t *testing.T, client pb.EngineClient, pool *pgxpool.Pool, id, runID string, types []string) {
	for ctx.Err() == nil {
		var state string
		if err := pool.QueryRow(ctx, `SELECT state FROM runs WHERE id = $1`, runID).Scan(&state); err != nil || state != "RUNNING" {
			return
		}
		lease, err := client.LeaseTask(ctx, &pb.LeaseRequest{WorkerId: id, TaskTypes: types})
		if err != nil {
			if ctx.Err() == nil {
				t.Errorf("LeaseTask: %v", err)
			}
			return
		}
		if !lease.HasTask {
			continue
		}
		_, err = client.Complete(ctx, &pb.CompleteRequest{
			TaskId: lease.TaskId, Attempt: lease.Attempt,
			Records: []*pb.Record{{Kind: "done", Key: lease.TaskType + "/" + lease.TaskKey, Data: []byte(`{"by":"test"}`)}},
		})
		if err != nil {
			t.Errorf("Complete: %v", err)
			return
		}
	}
}

// A diamond (a -> b, c -> d) must run in dependency order. The proof comes
// from the task_events log, not from what the workers saw.
func TestDiamondRunsInDependencyOrder(t *testing.T) {
	client, pool := startCoordinator(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	resp, err := client.SubmitRun(ctx, &pb.SubmitRunRequest{
		RunId: "diamond", Workflow: "test", Seed: 7,
		Tasks: []*pb.TaskSpec{
			{Type: "d", Key: "1", DependsOnKeys: []string{"b/1", "c/1"}},
			{Type: "b", Key: "1", DependsOnKeys: []string{"a/1"}},
			{Type: "c", Key: "1", DependsOnKeys: []string{"a/1"}},
			{Type: "a", Key: "1"},
		},
	})
	if err != nil || !resp.Created {
		t.Fatalf("SubmitRun: %v %v", resp, err)
	}

	var wg sync.WaitGroup
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			testWorker(ctx, t, client, pool, fmt.Sprintf("w%d", w), "diamond", []string{"a", "b", "c", "d"})
		}(w)
	}
	wg.Wait()

	var state string
	if err := pool.QueryRow(ctx, `SELECT state FROM runs WHERE id = 'diamond'`).Scan(&state); err != nil || state != "SUCCEEDED" {
		t.Fatalf("run state = %q err=%v, want SUCCEEDED", state, err)
	}

	// Invariant 2: a task's first LEASED event comes after the SUCCEEDED
	// event of every parent.
	var violations int
	err = pool.QueryRow(ctx, `
		SELECT count(*) FROM task_deps d
		WHERE (SELECT min(seq) FROM task_events WHERE task_id = d.child_id AND event = 'LEASED')
		    < (SELECT min(seq) FROM task_events WHERE task_id = d.parent_id AND event = 'SUCCEEDED')`).Scan(&violations)
	if err != nil || violations != 0 {
		t.Fatalf("%d children were leased before a parent succeeded (err=%v)", violations, err)
	}
	// Invariant 3: every task succeeded exactly once and wrote one record.
	var succeeded, records int
	_ = pool.QueryRow(ctx, `SELECT count(*) FROM task_events WHERE event = 'SUCCEEDED'`).Scan(&succeeded)
	_ = pool.QueryRow(ctx, `SELECT count(*) FROM records`).Scan(&records)
	if succeeded != 4 || records != 4 {
		t.Fatalf("SUCCEEDED events = %d, records = %d, want 4 and 4", succeeded, records)
	}
}

func TestSubmitRunRejectsCycle(t *testing.T) {
	client, _ := startCoordinator(t)
	_, err := client.SubmitRun(context.Background(), &pb.SubmitRunRequest{
		RunId: "bad", Workflow: "test",
		Tasks: []*pb.TaskSpec{
			{Type: "a", Key: "1", DependsOnKeys: []string{"b/1"}},
			{Type: "b", Key: "1", DependsOnKeys: []string{"a/1"}},
		},
	})
	if err == nil {
		t.Fatal("a cyclic DAG was accepted")
	}
}

func TestLeaseTaskLongPollReturnsEmpty(t *testing.T) {
	client, _ := startCoordinator(t)
	start := time.Now()
	resp, err := client.LeaseTask(context.Background(), &pb.LeaseRequest{WorkerId: "w", TaskTypes: []string{"none"}})
	if err != nil || resp.HasTask {
		t.Fatalf("LeaseTask: %v %v, want no task", resp, err)
	}
	if waited := time.Since(start); waited < 300*time.Millisecond {
		t.Fatalf("returned after %v; the long poll should wait about 500ms", waited)
	}
}
