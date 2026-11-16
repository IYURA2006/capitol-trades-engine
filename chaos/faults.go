package main

import (
	"context"
	"fmt"
	"math/rand/v2"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	project      = "capitol"
	postgresName = project + "-postgres-1"
)

var coordinatorNames = []string{project + "-coordinator-1-1", project + "-coordinator-2-1"}

// The four faults every run must contain, and two extra ones.
const (
	faultWorkerKill       = "worker_kill"
	faultZombie           = "zombie"
	faultCoordinatorKill  = "coordinator_kill"
	faultPostgresRestart  = "postgres_restart"
	faultBothCoordinators = "both_coordinators_down"
	faultPostgresStall    = "postgres_stall"
)

var requiredFaults = []string{faultWorkerKill, faultZombie, faultCoordinatorKill, faultPostgresRestart}

// FaultEvent is one line of the fault log. With the seed it lets us
// replay what happened to a failing run.
type FaultEvent struct {
	AtSeconds float64 `json:"at_s"` // since the run was submitted
	Fault     string  `json:"fault"`
	Target    string  `json:"target"`
	Action    string  `json:"action"`
}

func docker(args ...string) (string, error) {
	out, err := exec.Command("docker", args...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("docker %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return strings.TrimSpace(string(out)), nil
}

// workerContainers returns name -> short id (the hostname inside the
// container, and so the prefix of its worker_id).
func workerContainers() (map[string]string, error) {
	out, err := docker("ps", "-a", "--no-trunc",
		"--filter", "label=com.docker.compose.project="+project,
		"--filter", "label=com.docker.compose.service=worker",
		"--filter", "label=com.docker.compose.oneoff=False", // skip one-off helper containers
		"--format", "{{.Names}} {{.ID}}")
	if err != nil {
		return nil, err
	}
	workers := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		parts := strings.Fields(line)
		if len(parts) == 2 {
			workers[parts[0]] = parts[1][:12]
		}
	}
	if len(workers) < 3 {
		return nil, fmt.Errorf("chaos needs at least 3 workers, found %d", len(workers))
	}
	return workers, nil
}

// Injector runs the fault schedule for one chaos run.
type Injector struct {
	rng     *rand.Rand
	pool    *pgxpool.Pool
	runID   string
	lease   time.Duration
	start   time.Time
	workers map[string]string

	mu     sync.Mutex
	log    []FaultEvent
	busy   map[string]bool // containers a fault is using right now
	wg     sync.WaitGroup
	onLine func(string)
}

func (in *Injector) record(fault, target, action string) {
	ev := FaultEvent{AtSeconds: time.Since(in.start).Seconds(), Fault: fault, Target: target, Action: action}
	in.mu.Lock()
	in.log = append(in.log, ev)
	in.mu.Unlock()
	if in.onLine != nil {
		in.onLine(fmt.Sprintf("    %6.1fs  %-22s %-8s %s", ev.AtSeconds, fault, action, target))
	}
}

// act runs one docker command and logs it. A failed command is logged too:
// the fault log must show what really happened.
func (in *Injector) act(fault, target, action string) bool {
	if _, err := docker(action, target); err != nil {
		in.record(fault, target, action+" FAILED: "+err.Error())
		return false
	}
	in.record(fault, target, action)
	return true
}

// claim picks a free container from names (seeded choice) and marks it busy.
func (in *Injector) claim(names []string) (string, bool) {
	in.mu.Lock()
	defer in.mu.Unlock()
	var free []string
	for _, n := range names {
		if !in.busy[n] {
			free = append(free, n)
		}
	}
	if len(free) == 0 {
		return "", false
	}
	sort.Strings(free)
	pick := free[in.rng.IntN(len(free))]
	in.busy[pick] = true
	return pick, true
}

func (in *Injector) release(name string) {
	in.mu.Lock()
	delete(in.busy, name)
	in.mu.Unlock()
}

func (in *Injector) workerNames() []string {
	names := make([]string, 0, len(in.workers))
	for n := range in.workers {
		names = append(names, n)
	}
	return names
}

// leaseHolders returns the workers that hold a lease in this run right now.
// Freezing one of them makes a real zombie: a worker with a task in hand.
func (in *Injector) leaseHolders(ctx context.Context) []string {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	rows, err := in.pool.Query(ctx,
		`SELECT DISTINCT worker_id FROM tasks WHERE run_id = $1 AND state = 'LEASED' AND worker_id IS NOT NULL`, in.runID)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var holders []string
	for rows.Next() {
		var workerID string
		if rows.Scan(&workerID) != nil {
			continue
		}
		for name, id := range in.workers {
			if strings.HasPrefix(workerID, id) {
				holders = append(holders, name)
			}
		}
	}
	return holders
}

// inject starts one fault. Faults that take time run in the background so
// that several can overlap, as they would in production.
func (in *Injector) inject(ctx context.Context, fault string) {
	switch fault {
	case faultWorkerKill:
		target, ok := in.claim(in.workerNames())
		if !ok {
			return
		}
		in.act(fault, target, "kill")
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(target)
			time.Sleep(time.Second)
			in.act(fault, target, "start") // a new worker process with a new worker id
		}()

	case faultZombie:
		candidates := in.leaseHolders(ctx)
		target, ok := in.claim(candidates)
		if !ok {
			if target, ok = in.claim(in.workerNames()); !ok {
				return
			}
		}
		if !in.act(fault, target, "pause") {
			in.release(target)
			return
		}
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(target)
			time.Sleep(in.lease + 10*time.Second) // long enough for the lease to be given away
			in.act(fault, target, "unpause")
		}()

	case faultCoordinatorKill:
		target, ok := in.claim(coordinatorNames)
		if !ok {
			return
		}
		in.act(fault, target, "kill")
		down := time.Duration(2000+in.rng.IntN(2000)) * time.Millisecond // drawn here: rng is not for goroutines
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(target)
			time.Sleep(down)
			in.act(fault, target, "start")
		}()

	case faultBothCoordinators:
		first, ok1 := in.claim(coordinatorNames)
		second, ok2 := in.claim(coordinatorNames)
		if !ok1 || !ok2 {
			if ok1 {
				in.release(first)
			}
			return
		}
		in.act(fault, first, "kill")
		in.act(fault, second, "kill")
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(first)
			defer in.release(second)
			time.Sleep(15 * time.Second)
			in.act(fault, first, "start")
			in.act(fault, second, "start")
		}()

	case faultPostgresRestart:
		if _, ok := in.claim([]string{postgresName}); !ok {
			return
		}
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(postgresName)
			// -t 0: stop at once, like a crash. Postgres then recovers from its log.
			if _, err := docker("restart", "-t", "0", postgresName); err != nil {
				in.record(fault, postgresName, "restart FAILED: "+err.Error())
				return
			}
			in.record(fault, postgresName, "restart")
		}()

	case faultPostgresStall:
		if _, ok := in.claim([]string{postgresName}); !ok {
			return
		}
		if !in.act(fault, postgresName, "pause") {
			in.release(postgresName)
			return
		}
		in.wg.Add(1)
		go func() {
			defer in.wg.Done()
			defer in.release(postgresName)
			time.Sleep(5 * time.Second)
			in.act(fault, postgresName, "unpause")
		}()
	}
}

// schedule returns the faults of one run with their start times. The four
// required faults come first, in a seeded order, close together so they
// all happen while the run is still working. Extra faults follow.
func schedule(rng *rand.Rand) []struct {
	at    time.Duration
	fault string
} {
	order := append([]string(nil), requiredFaults...)
	rng.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
	if rng.Float64() < 0.5 {
		order = append(order, faultPostgresStall)
	}
	if rng.Float64() < 0.3 {
		order = append(order, faultBothCoordinators)
	}
	order = append(order, faultWorkerKill, faultZombie) // a second round if the run is still going

	var plan []struct {
		at    time.Duration
		fault string
	}
	at := time.Duration(800+rng.IntN(700)) * time.Millisecond
	for _, f := range order {
		plan = append(plan, struct {
			at    time.Duration
			fault string
		}{at, f})
		at += time.Duration(1200+rng.IntN(1300)) * time.Millisecond
	}
	return plan
}

// heal brings every container back to "running". It is called after the
// faults of a run are over, so the next run starts from a healthy stack.
func heal() error {
	out, err := docker("ps", "-a", "--filter", "label=com.docker.compose.project="+project,
		"--format", "{{.Names}} {{.State}}")
	if err != nil {
		return err
	}
	for _, line := range strings.Split(out, "\n") {
		parts := strings.Fields(line)
		if len(parts) != 2 || strings.Contains(parts[0], "-run-") {
			continue
		}
		switch parts[1] {
		case "paused":
			if _, err := docker("unpause", parts[0]); err != nil {
				return err
			}
		case "exited", "created", "dead":
			if _, err := docker("start", parts[0]); err != nil {
				return err
			}
		}
	}
	return nil
}
