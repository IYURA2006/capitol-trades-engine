// Command ctl submits runs and shows their status.
//
//	ctl submit -run r1 -workflow toy -file dag.json [-snapshot s1] [-seed 42]
//	ctl status -run r1
//	ctl rate   -key house-clerk -interval 2s
//
// submit talks to a coordinator over gRPC. status and rate read or write
// Postgres through the store package.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"capitoltrades/coordinator/internal/pb"
	"capitoltrades/coordinator/internal/store"
)

const maxMessageBytes = 16 << 20

// fileTask is one task in the JSON file given to `ctl submit`.
type fileTask struct {
	Type             string          `json:"type"`
	Key              string          `json:"key"`
	Payload          json.RawMessage `json:"payload"`
	DependsOn        []string        `json:"depends_on"`
	RateKey          string          `json:"rate_key"`
	TolerateFailures bool            `json:"tolerate_failures"`
	MaxAttempts      int32           `json:"max_attempts"`
}

func main() {
	if len(os.Args) < 2 {
		fail("usage: ctl submit|status|rate [flags]")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	var err error
	switch os.Args[1] {
	case "submit":
		err = submit(ctx, os.Args[2:])
	case "status":
		err = status(ctx, os.Args[2:])
	case "rate":
		err = rate(ctx, os.Args[2:])
	default:
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		fail(err.Error())
	}
}

func submit(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("submit", flag.ExitOnError)
	addrs := fs.String("coordinators", env("COORDINATORS", "localhost:50051,localhost:50052"), "comma-separated gRPC addresses")
	runID := fs.String("run", "", "run id (required)")
	workflow := fs.String("workflow", "", "workflow name (required)")
	file := fs.String("file", "", "JSON file with a list of tasks (required)")
	snapshot := fs.String("snapshot", "", "snapshot id; empty means live mode")
	seed := fs.Int64("seed", 0, "seed for all randomness in the run")
	_ = fs.Parse(args)

	raw, err := os.ReadFile(*file)
	if err != nil {
		return err
	}
	var tasks []fileTask
	if err := json.Unmarshal(raw, &tasks); err != nil {
		return fmt.Errorf("%s: %w", *file, err)
	}
	req := &pb.SubmitRunRequest{RunId: *runID, Workflow: *workflow, SnapshotId: *snapshot, Seed: *seed}
	for _, t := range tasks {
		req.Tasks = append(req.Tasks, &pb.TaskSpec{
			Type: t.Type, Key: t.Key, Payload: t.Payload, DependsOnKeys: t.DependsOn,
			RateKey: t.RateKey, TolerateFailures: t.TolerateFailures, MaxAttempts: t.MaxAttempts,
		})
	}

	// SubmitRun is idempotent, so trying the next coordinator is safe.
	var lastErr error
	for _, addr := range strings.Split(*addrs, ",") {
		conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()),
			grpc.WithDefaultCallOptions(grpc.MaxCallSendMsgSize(maxMessageBytes)))
		if err != nil {
			lastErr = err
			continue
		}
		resp, err := pb.NewEngineClient(conn).SubmitRun(ctx, req)
		_ = conn.Close()
		if err != nil {
			lastErr = err
			continue
		}
		if resp.Created {
			fmt.Printf("run %s created with %d tasks\n", *runID, len(req.Tasks))
		} else {
			fmt.Printf("run %s already exists; nothing changed\n", *runID)
		}
		return nil
	}
	return fmt.Errorf("no coordinator accepted the run: %w", lastErr)
}

func status(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("status", flag.ExitOnError)
	runID := fs.String("run", "", "run id (required)")
	_ = fs.Parse(args)

	s, closePool, err := openStore(ctx)
	if err != nil {
		return err
	}
	defer closePool()

	state, err := s.RunState(ctx, *runID)
	if err != nil {
		return err
	}
	counts, err := s.StateCounts(ctx, *runID)
	if err != nil {
		return err
	}
	fmt.Printf("run %s: %s\n", *runID, state)
	fmt.Printf("%-24s %-12s %s\n", "TYPE", "STATE", "COUNT")
	for _, c := range counts {
		fmt.Printf("%-24s %-12s %d\n", c.Type, c.State, c.Count)
	}
	return nil
}

func rate(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("rate", flag.ExitOnError)
	key := fs.String("key", "", "rate key (required)")
	interval := fs.Duration("interval", 2*time.Second, "minimum gap between two leases")
	_ = fs.Parse(args)

	s, closePool, err := openStore(ctx)
	if err != nil {
		return err
	}
	defer closePool()
	if err := s.SetRateLimit(ctx, *key, *interval); err != nil {
		return err
	}
	fmt.Printf("rate key %s: one lease every %s\n", *key, *interval)
	return nil
}

func openStore(ctx context.Context) (*store.Store, func(), error) {
	pool, err := pgxpool.New(ctx, env("DATABASE_URL", "postgres://engine:engine@localhost:5433/engine?sslmode=disable"))
	if err != nil {
		return nil, nil, err
	}
	return store.New(pool, store.Config{}), pool.Close, nil
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "ctl: "+msg)
	os.Exit(1)
}
