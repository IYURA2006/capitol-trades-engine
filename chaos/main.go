// Command chaos runs a workflow many times while it kills, freezes and
// restarts parts of the system, and checks every run against a
// single-process reference.
//
//	chaos run   -n 100 -workflow toy -param n=2000
//	chaos check -run <run id> -ref reference.json
//
// It drives the Docker CLI, so the Compose stack must be up (`make chaos`
// does that). Every run prints its seed; `-seed S -n 1` replays one run.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type params []string

func (p *params) String() string     { return strings.Join(*p, ",") }
func (p *params) Set(v string) error { *p = append(*p, v); return nil }

// RunReport is one line of the results file.
type RunReport struct {
	RunID       string         `json:"run_id"`
	Seed        int64          `json:"seed"`
	Seconds     float64        `json:"seconds"`
	Faults      map[string]int `json:"faults"`
	FaultsInRun map[string]int `json:"faults_while_running"`
	FaultLog    []FaultEvent   `json:"fault_log"`
	Check       *CheckResult   `json:"check"`
	Error       string         `json:"error,omitempty"`
	// Counted is false when the run is correct but did not test enough:
	// a required fault came after the run ended, or no stale request was
	// rejected (so fencing was not exercised).
	Counted   bool   `json:"counted"`
	NotReason string `json:"not_counted_reason,omitempty"`
}

func (r *RunReport) Passed() bool {
	return r.Error == "" && r.Check != nil && len(r.Check.Violations) == 0
}

func main() {
	if len(os.Args) < 2 {
		fatal("usage: chaos run|check [flags]")
	}
	switch os.Args[1] {
	case "run":
		os.Exit(cmdRun(os.Args[2:]))
	case "check":
		os.Exit(cmdCheck(os.Args[2:]))
	default:
		fatal("unknown command " + os.Args[1])
	}
}

func cmdCheck(args []string) int {
	fs := flag.NewFlagSet("check", flag.ExitOnError)
	dsn := fs.String("db", env("DATABASE_URL", "postgres://engine:engine@localhost:5433/engine?sslmode=disable"), "Postgres URL")
	runID := fs.String("run", "", "run id")
	refPath := fs.String("ref", "", "reference JSON from `capitol_sdk.cli local`")
	_ = fs.Parse(args)

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		fatal(err.Error())
	}
	defer pool.Close()
	ref, err := loadReference(*refPath)
	if err != nil {
		fatal(err.Error())
	}
	res, err := check(ctx, pool, *runID, ref)
	if err != nil {
		fatal(err.Error())
	}
	out, _ := json.MarshalIndent(res, "", "  ")
	fmt.Println(string(out))
	if len(res.Violations) > 0 {
		return 1
	}
	return 0
}

type runConfig struct {
	workflow, snapshot, modules, outDir string
	params, refParams                   params
	lease, timeout                      time.Duration
	pool                                *pgxpool.Pool
	workflowSeed                        int64
	fixedSeed                           bool
	sharedRef                           string
}

func cmdRun(args []string) int {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	cfg := runConfig{}
	dsn := fs.String("db", env("DATABASE_URL", "postgres://engine:engine@localhost:5433/engine?sslmode=disable"), "Postgres URL")
	n := fs.Int("n", 100, "number of counted runs wanted")
	baseSeed := fs.Int64("seed", 1, "seed of the first run; run i uses seed+i")
	fs.StringVar(&cfg.workflow, "workflow", "toy", "workflow name")
	fs.StringVar(&cfg.snapshot, "snapshot", "", "snapshot id (required for real pipelines)")
	fs.StringVar(&cfg.modules, "modules", "pipeline.tasks.toy", "Python modules with the tasks")
	fs.StringVar(&cfg.outDir, "out", "chaos/results", "directory for the results file")
	fs.Var(&cfg.params, "param", "workflow parameter name=value (repeatable)")
	fs.Var(&cfg.refParams, "ref-param", "extra parameter for the reference run only (repeatable)")
	workflowSeed := fs.Int64("workflow-seed", -1,
		"use this seed for the workflow in every run (the fault schedule still changes); the reference is then computed once")
	var rates params
	fs.Var(&rates, "rate", "rate limit key=interval, for example house-clerk=20ms (repeatable)")
	fs.DurationVar(&cfg.lease, "lease", 5*time.Second, "lease length the coordinators use")
	fs.DurationVar(&cfg.timeout, "timeout", 10*time.Minute, "give up on a run after this long")
	_ = fs.Parse(args)

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		fatal(err.Error())
	}
	defer pool.Close()
	cfg.pool = pool

	// A snapshot answers at once, so replay runs may use a much shorter
	// interval than the 2 s we keep for the real website. The limit itself is
	// still enforced and checked (invariant 7).
	for _, r := range rates {
		key, interval, ok := strings.Cut(r, "=")
		d, err := time.ParseDuration(interval)
		if !ok || err != nil {
			fatal("bad -rate " + r)
		}
		if _, err := pool.Exec(ctx, `INSERT INTO rate_limits (key, min_interval) VALUES ($1, $2::interval)
			ON CONFLICT (key) DO UPDATE SET min_interval = EXCLUDED.min_interval`, key, d); err != nil {
			fatal(err.Error())
		}
	}

	if err := os.MkdirAll(cfg.outDir, 0o755); err != nil {
		fatal(err.Error())
	}
	stamp := time.Now().UTC().Format("20060102-150405")
	resultsPath := filepath.Join(cfg.outDir, fmt.Sprintf("%s-%s.jsonl", cfg.workflow, stamp))
	results, err := os.Create(resultsPath)
	if err != nil {
		fatal(err.Error())
	}
	defer results.Close()

	var reports []*RunReport
	counted, failed := 0, 0
	maxRuns := *n + *n/2 + 3 // some runs may not count; do not loop forever
	for i := 0; counted < *n && i < maxRuns; i++ {
		seed := *baseSeed + int64(i)
		runID := fmt.Sprintf("chaos-%s-%s-%d", cfg.workflow, stamp, seed)
		fmt.Printf("run %d (seed %d, %d/%d counted)\n", i+1, seed, counted, *n)
		cfg.workflowSeed, cfg.fixedSeed = seed, false
		if *workflowSeed >= 0 {
			cfg.workflowSeed, cfg.fixedSeed = *workflowSeed, true
			cfg.sharedRef = filepath.Join(cfg.outDir, fmt.Sprintf("%s-%s.reference.json", cfg.workflow, stamp))
		}
		report := oneRun(ctx, cfg, runID, seed)
		reports = append(reports, report)
		line, _ := json.Marshal(report)
		_, _ = results.Write(append(line, '\n'))

		switch {
		case !report.Passed():
			failed++
			fmt.Printf("  FAILED: %s %v\n", report.Error, violations(report))
		case report.Counted:
			counted++
			fmt.Printf("  ok in %.0fs: stale rejected %d (complete %d), expiries %d, hash %.12s\n",
				report.Seconds, report.Check.Stale(), report.Check.StaleComplete, report.Check.Expiries, report.Check.Hash)
		default:
			fmt.Printf("  correct but not counted: %s\n", report.NotReason)
		}
	}

	printSummary(reports, *n, resultsPath)
	if failed > 0 || counted < *n {
		return 1
	}
	return 0
}

func violations(r *RunReport) []string {
	if r.Check == nil {
		return nil
	}
	return r.Check.Violations
}

// oneRun computes the reference, submits the run, injects faults until
// the run ends, waits for the faults to heal, and checks the invariants.
func oneRun(ctx context.Context, cfg runConfig, runID string, seed int64) *RunReport {
	report := &RunReport{RunID: runID, Seed: seed, Faults: map[string]int{}, FaultsInRun: map[string]int{}}
	fail := func(err error) *RunReport {
		report.Error = err.Error()
		_ = heal()
		return report
	}

	if err := heal(); err != nil {
		return fail(err)
	}
	if err := waitHealthy(ctx, cfg.pool); err != nil {
		return fail(err)
	}

	refPath := filepath.Join(cfg.outDir, runID+".reference.json")
	if cfg.fixedSeed {
		refPath = cfg.sharedRef
	}
	if _, err := os.Stat(refPath); err != nil {
		if err := sdk(refPath, cfg, "local", cfg.workflowSeed, append(append(params{}, cfg.params...), cfg.refParams...)); err != nil {
			return fail(fmt.Errorf("reference run: %w", err))
		}
	}
	ref, err := loadReference(refPath)
	if err != nil {
		return fail(err)
	}
	if err := sdk("", cfg, "submit", cfg.workflowSeed, cfg.params, "--run", runID,
		"--coordinators", "coordinator-1:50051,coordinator-2:50051"); err != nil {
		return fail(fmt.Errorf("submit: %w", err))
	}

	workers, err := workerContainers()
	if err != nil {
		return fail(err)
	}
	injector := &Injector{
		rng:  rand.New(rand.NewPCG(uint64(seed), 0x636861)), // every choice comes from the seed
		pool: cfg.pool, runID: runID, lease: cfg.lease, start: time.Now(),
		workers: workers, busy: map[string]bool{},
		onLine: func(s string) { fmt.Println(s) },
	}
	plan := schedule(injector.rng)
	next := 0
	deadline := injector.start.Add(cfg.timeout)
	for {
		state := runState(ctx, cfg.pool, runID)
		if state == "SUCCEEDED" || state == "FAILED" {
			break
		}
		if time.Now().After(deadline) {
			injector.wg.Wait()
			return fail(fmt.Errorf("run did not finish in %s", cfg.timeout))
		}
		if next < len(plan) && time.Since(injector.start) >= plan[next].at {
			injector.inject(ctx, plan[next].fault)
			report.FaultsInRun[plan[next].fault]++
			next++
		}
		time.Sleep(100 * time.Millisecond)
	}
	report.Seconds = time.Since(injector.start).Seconds()

	// The run is over, but a zombie may still be frozen. Let every fault
	// finish, heal, and give woken zombies time to send their stale requests.
	injector.wg.Wait()
	if err := heal(); err != nil {
		return fail(err)
	}
	if err := waitHealthy(ctx, cfg.pool); err != nil {
		return fail(err)
	}
	time.Sleep(3 * time.Second)

	report.FaultLog = injector.log
	for _, ev := range injector.log {
		if ev.Action == "kill" || ev.Action == "pause" || ev.Action == "restart" {
			report.Faults[ev.Fault]++
		}
	}
	report.Check, err = check(ctx, cfg.pool, runID, ref)
	if err != nil {
		return fail(fmt.Errorf("checker: %w", err))
	}

	report.Counted = true
	for _, f := range requiredFaults {
		if report.FaultsInRun[f] == 0 || report.Faults[f] == 0 {
			report.Counted, report.NotReason = false, "the run ended before fault "+f
		}
	}
	if report.Counted && report.Check.Stale() == 0 {
		report.Counted, report.NotReason = false, "no stale request was rejected, so fencing was not exercised"
	}
	if !cfg.fixedSeed {
		_ = os.Remove(refPath) // keep the directory small; the hash is in the report
	}
	return report
}

// sdk runs `python -m capitol_sdk.cli` in a fresh worker container, so the
// reference uses exactly the same code and library versions as the workers.
func sdk(outPath string, cfg runConfig, command string, seed int64, ps params, extra ...string) error {
	args := []string{"compose", "run", "--rm", "--no-deps", "-T", "worker",
		"python", "-m", "capitol_sdk.cli", command,
		"--workflow", cfg.workflow, "--seed", fmt.Sprint(seed), "--modules", cfg.modules}
	if cfg.snapshot != "" {
		args = append(args, "--snapshot", cfg.snapshot)
	}
	for _, p := range ps {
		args = append(args, "--param", p)
	}
	args = append(args, extra...)
	cmd := exec.Command("docker", args...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(stderr.String()))
	}
	if outPath != "" {
		return os.WriteFile(outPath, out, 0o644)
	}
	return nil
}

// runState returns "" while Postgres cannot be reached. That is normal
// during a Postgres fault, so the caller just asks again.
func runState(ctx context.Context, pool *pgxpool.Pool, runID string) string {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var state string
	if err := pool.QueryRow(ctx, `SELECT state FROM runs WHERE id = $1`, runID).Scan(&state); err != nil {
		return ""
	}
	return state
}

// waitHealthy waits until Postgres answers and both coordinators serve.
func waitHealthy(ctx context.Context, pool *pgxpool.Pool) error {
	deadline := time.Now().Add(90 * time.Second)
	for {
		pingCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		err := pool.Ping(pingCtx)
		cancel()
		if err == nil {
			err = coordinatorsHealthy()
		}
		if err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("stack not healthy: %w", err)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func coordinatorsHealthy() error {
	for _, name := range coordinatorNames {
		if _, err := docker("exec", name, "wget", "-qO-", "http://localhost:8080/healthz"); err != nil {
			return err
		}
	}
	return nil
}

func printSummary(reports []*RunReport, wanted int, resultsPath string) {
	var counted, notCounted, failed, faults, stale, staleComplete, expiries int
	byFault := map[string]int{}
	var seconds float64
	for _, r := range reports {
		switch {
		case !r.Passed():
			failed++
		case r.Counted:
			counted++
		default:
			notCounted++
		}
		for f, n := range r.Faults {
			byFault[f] += n
			faults += n
		}
		if r.Check != nil {
			stale += r.Check.Stale()
			staleComplete += r.Check.StaleComplete
			expiries += r.Check.Expiries
		}
		seconds += r.Seconds
	}
	fmt.Println()
	fmt.Println("| Chaos summary | |")
	fmt.Println("| --- | --- |")
	fmt.Printf("| Runs started | %d |\n", len(reports))
	fmt.Printf("| Runs that match the reference and count | %d of %d wanted |\n", counted, wanted)
	fmt.Printf("| Runs correct but not counted | %d |\n", notCounted)
	fmt.Printf("| Runs with a violation or mismatch | %d |\n", failed)
	fmt.Printf("| Faults injected | %d |\n", faults)
	for _, f := range []string{faultWorkerKill, faultZombie, faultCoordinatorKill, faultPostgresRestart,
		faultPostgresStall, faultBothCoordinators} {
		fmt.Printf("| - %s | %d |\n", f, byFault[f])
	}
	fmt.Printf("| Lease expiries | %d |\n", expiries)
	fmt.Printf("| Stale requests rejected | %d (Complete: %d) |\n", stale, staleComplete)
	fmt.Printf("| Run time, sum | %.0f s |\n", seconds)
	fmt.Printf("\nresults: %s\n", resultsPath)
	for _, r := range reports {
		if !r.Passed() {
			fmt.Printf("FAILED run %s seed %d: %s %v\n", r.RunID, r.Seed, r.Error, violations(r))
		}
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func fatal(msg string) {
	fmt.Fprintln(os.Stderr, "chaos: "+msg)
	os.Exit(2)
}
