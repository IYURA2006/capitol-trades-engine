package engine

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// Prometheus metrics. Each coordinator exports its own counters; add them
// up across coordinators to get the totals. The gauges come from Postgres,
// so both coordinators report the same values.
var (
	mTasks = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "engine_tasks", Help: "Tasks that are not finished, by state (from Postgres).",
	}, []string{"state"})

	mLeaseQuery = promauto.NewHistogram(prometheus.HistogramOpts{
		Name: "engine_lease_query_seconds", Help: "Time of one lease query in Postgres.",
		Buckets: prometheus.ExponentialBuckets(0.0005, 2, 12),
	})

	mTaskDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "engine_task_duration_seconds", Help: "Time from lease to successful Complete.",
		Buckets: prometheus.ExponentialBuckets(0.01, 2, 14),
	}, []string{"type"})

	mLeased       = promauto.NewCounter(prometheus.CounterOpts{Name: "engine_leases_total", Help: "Tasks handed to workers."})
	mCompleted    = promauto.NewCounter(prometheus.CounterOpts{Name: "engine_completed_total", Help: "Tasks completed."})
	mRetries      = promauto.NewCounter(prometheus.CounterOpts{Name: "engine_retries_total", Help: "Explicit failures that will be retried."})
	mExpiries     = promauto.NewCounter(prometheus.CounterOpts{Name: "engine_lease_expiries_total", Help: "Leases that ran out."})
	mDeadLetters  = promauto.NewCounter(prometheus.CounterOpts{Name: "engine_dead_letters_total", Help: "Tasks moved to the dead-letter state."})
	mStaleRejects = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "engine_stale_rejections_total", Help: "Requests refused because the fencing token was old.",
	}, []string{"rpc"})
)
