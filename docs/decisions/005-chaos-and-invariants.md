# 005: Chaos harness and invariant checker

**Decision.** Correctness is tested from the outside. A Go program starts a run, then kills, freezes and restarts containers on a seeded schedule. When the run ends, a checker reads Postgres and tests seven invariants, then compares the run with a single-process reference (`run_local`) of the same workflow and seed.

**Alternatives.** Unit tests with mocked failures, or a model checker such as TLA+.

**Why.** Mocks only test the failures I thought of. Real `docker kill`, `pause` and a Postgres restart also produce the ones I did not. The reference run shares no engine code, so an engine bug cannot hide in both.

**Details that matter.**

- Each run must contain a worker kill, a zombie, a coordinator kill and a Postgres restart while it is still working, and at least one rejected stale request. Otherwise it is "correct but not counted".
- The zombie is chosen from workers that hold a lease, so it wakes up with a task in hand.
- The reference runs inside the worker image, so both sides use the same library versions.
- Records are compared as `jsonb` in Postgres, and by a hash of their text.
- The checker has its own test: a small correct run is damaged in 15 ways and every kind of damage must be reported.

**Limit.** Passing runs show that no violation happened in these schedules. They do not prove that none is possible.
