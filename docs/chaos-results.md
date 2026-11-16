# Chaos results

Raw per-run reports (seed, fault log, checker output) for both batches are in `docs/chaos-raw/`. New runs write to `chaos/results/`.

## Toy workflow: 100 runs (2026-11-12)

`make chaos N=100 SEED=1000`: 2,000 hash tasks plus a join, 6 workers, 2 coordinators, lease 5 s. Each run has its own seed for both the workflow and the fault schedule, and its own reference.

| Chaos summary | |
| --- | --- |
| Runs started | 100 |
| Runs that match the reference and count | 100 of 100 wanted |
| Runs correct but not counted | 0 |
| Runs with a violation or mismatch | 0 |
| Faults injected | 703 |
| - worker_kill | 200 |
| - zombie | 200 |
| - coordinator_kill | 100 |
| - postgres_restart | 100 |
| - postgres_stall | 55 |
| - both_coordinators_down | 48 |
| Lease expiries | 464 |
| Stale requests rejected | 429 (Complete: 174) |
| Run time, sum | 3529 s |

## Real pipeline: 100 runs (2026-11-13)

`make chaos-real N=100 SEED=2000`: the House pipeline replayed from the full snapshot (966 filings; 3,153 tasks and 23,016 records per run), 6 workers, 2 coordinators, lease 5 s.

Difference from the toy batch: the workflow seed is the same (7) in every run, so one reference serves all 100 runs; computing it takes about 2.5 minutes. The fault schedule has a new seed in every run. All 100 runs ended with the same results hash, `f6b51d23ff2f...`, equal to the reference.

| Chaos summary | |
| --- | --- |
| Runs started | 100 |
| Runs that match the reference and count | 100 of 100 wanted |
| Runs correct but not counted | 0 |
| Runs with a violation or mismatch | 0 |
| Faults injected | 703 |
| - worker_kill | 200 |
| - zombie | 200 |
| - coordinator_kill | 100 |
| - postgres_restart | 100 |
| - postgres_stall | 45 |
| - both_coordinators_down | 58 |
| Lease expiries | 434 |
| Stale requests rejected | 438 (Complete: 182) |
| Run time, sum | 14433 s |

## Totals

200 runs, 1,406 faults injected, 898 lease expiries, 867 stale requests rejected (356 on `Complete`), 0 mismatches.
