package store

import (
	"context"

	"github.com/jackc/pgx/v5"
)

type RunInfo struct {
	ID, Workflow, State, Snapshot, Created string
}

type EventInfo struct {
	At, Type, Key string
	Attempt       int
	Event, Detail string
}

type DeadLetter struct {
	Type, Key                   string
	Attempt, Failures, Expiries int
	Error                       string
}

// Status is everything the status page shows. It is read straight from
// Postgres on every request, so both coordinators show the same page.
type Status struct {
	Runs        []RunInfo
	Selected    string
	Counts      []StateCount
	Events      []EventInfo
	DeadLetters []DeadLetter
}

// ReadStatus returns the latest runs and the details of one run
// (runID, or the newest run when runID is empty).
func (s *Store) ReadStatus(ctx context.Context, runID string) (*Status, error) {
	st := &Status{}
	rows, err := s.pool.Query(ctx, sqlStatusRuns, 8)
	if err != nil {
		return nil, err
	}
	st.Runs, err = pgx.CollectRows(rows, func(r pgx.CollectableRow) (RunInfo, error) {
		var x RunInfo
		return x, r.Scan(&x.ID, &x.Workflow, &x.State, &x.Snapshot, &x.Created)
	})
	if err != nil || len(st.Runs) == 0 {
		return st, err
	}
	st.Selected = runID
	if st.Selected == "" {
		st.Selected = st.Runs[0].ID
	}

	rows, err = s.pool.Query(ctx, sqlStatusCounts, st.Selected)
	if err != nil {
		return nil, err
	}
	st.Counts, err = pgx.CollectRows(rows, func(r pgx.CollectableRow) (StateCount, error) {
		var x StateCount
		return x, r.Scan(&x.Type, &x.State, &x.Count)
	})
	if err != nil {
		return nil, err
	}

	rows, err = s.pool.Query(ctx, sqlStatusEvents, st.Selected, 25)
	if err != nil {
		return nil, err
	}
	st.Events, err = pgx.CollectRows(rows, func(r pgx.CollectableRow) (EventInfo, error) {
		var x EventInfo
		return x, r.Scan(&x.At, &x.Type, &x.Key, &x.Attempt, &x.Event, &x.Detail)
	})
	if err != nil {
		return nil, err
	}

	rows, err = s.pool.Query(ctx, sqlStatusDeadLetters, st.Selected, 25)
	if err != nil {
		return nil, err
	}
	st.DeadLetters, err = pgx.CollectRows(rows, func(r pgx.CollectableRow) (DeadLetter, error) {
		var x DeadLetter
		return x, r.Scan(&x.Type, &x.Key, &x.Attempt, &x.Failures, &x.Expiries, &x.Error)
	})
	return st, err
}
