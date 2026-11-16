// Package engine holds the rules of the workflow engine that are not SQL:
// DAG validation, task ids, backoff, the lease long-poll and the reaper loop.
package engine

import (
	"context"
	"errors"
	"fmt"
	"log"
	"math/rand/v2"
	"time"

	"capitoltrades/coordinator/internal/store"
)

// ErrInvalid marks a request that can never succeed, however often it is retried.
var ErrInvalid = errors.New("invalid request")

type Config struct {
	Lease          time.Duration // how long a worker owns a task without a heartbeat
	LongPoll       time.Duration // how long LeaseTask waits for work
	PollInterval   time.Duration // gap between database polls inside LeaseTask
	ReaperInterval time.Duration
	BackoffBase    time.Duration
	BackoffCap     time.Duration
}

func DefaultConfig() Config {
	return Config{
		Lease:          30 * time.Second,
		LongPoll:       20 * time.Second,
		PollInterval:   250 * time.Millisecond,
		ReaperInterval: 2 * time.Second,
		BackoffBase:    time.Second,
		BackoffCap:     60 * time.Second,
	}
}

type Engine struct {
	store *store.Store
	cfg   Config
}

func New(s *store.Store, cfg Config) *Engine { return &Engine{store: s, cfg: cfg} }

func (e *Engine) LeaseDuration() time.Duration { return e.cfg.Lease }

func (e *Engine) SubmitRun(ctx context.Context, run store.Run, specs []Spec) (created bool, err error) {
	if run.ID == "" || run.Workflow == "" {
		return false, fmt.Errorf("%w: run_id and workflow are required", ErrInvalid)
	}
	if len(specs) == 0 {
		return false, fmt.Errorf("%w: a run needs at least one task", ErrInvalid)
	}
	tasks, err := BuildDAG(run.ID, specs)
	if err != nil {
		return false, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	return e.store.SubmitRun(ctx, run, tasks)
}

// LeaseTask waits up to LongPoll for a task. It polls Postgres with jitter
// so many workers do not all ask at the same instant.
func (e *Engine) LeaseTask(ctx context.Context, workerID string, types []string) (*store.Leased, error) {
	if workerID == "" || len(types) == 0 {
		return nil, fmt.Errorf("%w: worker_id and task_types are required", ErrInvalid)
	}
	deadline := time.Now().Add(e.cfg.LongPoll)
	for {
		start := time.Now()
		leased, err := e.store.Lease(ctx, workerID, types, e.cfg.Lease)
		mLeaseQuery.Observe(time.Since(start).Seconds())
		if leased != nil {
			mLeased.Inc()
		}
		if err != nil || leased != nil {
			return leased, err
		}
		wait := jitter(e.cfg.PollInterval)
		if time.Now().Add(wait).After(deadline) {
			return nil, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(wait):
		}
	}
}

func (e *Engine) Heartbeat(ctx context.Context, taskID string, attempt int64) (alive bool, err error) {
	alive, err = e.store.Heartbeat(ctx, taskID, attempt, e.cfg.Lease)
	if err == nil && !alive {
		mStaleRejects.WithLabelValues("Heartbeat").Inc()
	}
	return alive, err
}

// ObserveTaskDone is the store's OnTaskDone hook.
func ObserveTaskDone(taskType string, sinceLease time.Duration) {
	mTaskDuration.WithLabelValues(taskType).Observe(sinceLease.Seconds())
}

func (e *Engine) Complete(ctx context.Context, taskID string, attempt int64, output []byte, records []store.Record, subDAG []Spec) (store.Outcome, error) {
	var children []store.NewTask
	if len(subDAG) > 0 {
		// run_id never changes, so reading it outside the transaction is safe.
		runID, err := e.store.TaskRun(ctx, taskID)
		if err != nil {
			return store.OK, err
		}
		children, err = BuildDAG(runID, subDAG)
		if err != nil {
			return store.OK, fmt.Errorf("%w: sub-DAG: %v", ErrInvalid, err)
		}
	}
	outcome, err := e.store.Complete(ctx, taskID, attempt, output, records, children)
	if err == nil {
		if outcome == store.Stale {
			mStaleRejects.WithLabelValues("Complete").Inc()
		} else {
			mCompleted.Inc()
		}
	}
	return outcome, err
}

func (e *Engine) Fail(ctx context.Context, taskID string, attempt int64, errMsg string, fatal bool, retryAfter time.Duration) (store.Outcome, error) {
	outcome, err := e.store.Fail(ctx, taskID, attempt, errMsg, fatal, func(failures int) time.Duration {
		if retryAfter > 0 {
			return retryAfter // the remote server told us how long to wait
		}
		return Backoff(e.cfg.BackoffBase, e.cfg.BackoffCap, failures, rand.Float64())
	})
	if err != nil {
		return outcome, err
	}
	switch outcome {
	case store.Stale:
		mStaleRejects.WithLabelValues("Fail").Inc()
	case store.Dead:
		mDeadLetters.Inc()
		outcome = store.OK
	default:
		mRetries.Inc()
	}
	return outcome, nil
}

// Backoff returns min(cap, base * 2^failures) scaled by a factor in
// [0.5, 1.0]. r is a random number in [0, 1). The random factor stops
// tasks that failed together from all retrying at the same moment.
func Backoff(base, limit time.Duration, failures int, r float64) time.Duration {
	d := base
	for i := 0; i < failures && d < limit; i++ {
		d *= 2
	}
	if d > limit {
		d = limit
	}
	return time.Duration(float64(d) * (0.5 + r/2))
}

// RunReaper calls Reap until ctx ends. Every coordinator runs it; the SQL
// is idempotent, so they do not need to agree on who does it.
func (e *Engine) RunReaper(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(jitter(e.cfg.ReaperInterval)):
		}
		res, err := e.store.Reap(ctx)
		if err != nil {
			if ctx.Err() == nil {
				log.Printf("reaper: %v", err)
			}
			continue
		}
		mExpiries.Add(float64(res.Expired))
		mDeadLetters.Add(float64(res.DeadLettered))
		if counts, err := e.store.ActiveCounts(ctx); err == nil {
			for state, n := range counts {
				mTasks.WithLabelValues(state).Set(float64(n))
			}
		}
		if res.Expired > 0 || res.RunsClosed > 0 {
			log.Printf("reaper: %d leases expired (%d dead-lettered), %d runs closed",
				res.Expired, res.DeadLettered, res.RunsClosed)
		}
	}
}

// jitter returns a duration between 0.75*d and 1.25*d.
func jitter(d time.Duration) time.Duration {
	return time.Duration(float64(d) * (0.75 + rand.Float64()/2))
}
