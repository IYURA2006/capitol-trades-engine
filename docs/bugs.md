# Bugs found by tests

Each entry: symptom, cause, fix, and the test that now catches it.

## 1. Frozen-worker test failed 3 times out of 5

- **Found by:** `tests/integration/test_failures.py::test_frozen_worker_wakes_up_and_is_rejected` (Phase 3), when repeated.
- **Symptom.** The test waited for a `STALE_REJECTED` event after `docker unpause` and timed out. No wrong data was written; the event was simply missing.
- **Cause.** A wrong assumption in the plan, not a data bug. When a frozen worker wakes up, two things race: the task result arriving from the child process, and the heartbeat thread. If the heartbeat wins, the coordinator answers "lease lost", the worker kills the task and never sends `Complete`. That is correct behaviour, but a stale heartbeat left no trace in `task_events`, so the test could not see that fencing had worked.
- **Fix.** A rejected heartbeat now writes a `STALE_REJECTED` event with `rpc: Heartbeat`. The freeze test accepts either path and still requires that every record comes from the new attempt.
- **Tests now.** The freeze test (either path), plus `test_complete_with_old_fencing_token_gets_stale_lease`, where the test itself is the zombie, so the `Complete` path is checked on every run with no race.

## 2. The results hash can differ for equal numbers

- **Found by:** `chaos/check_test.go`, the checker's own test, on a run that was correct (Phase 5).
- **Symptom.** The checker reported "results hash differs from the reference" although every record was equal.
- **Cause.** The hash is taken over the text of each `jsonb` value, and Postgres keeps the digits as written: `0.5` and `0.50` are equal numbers but different text.
- **Fix.** No engine change. Workers and `run_local` both write records with the same function (`canonical_json`), so the text is the same. The record-by-record comparison uses `jsonb` equality; the hash stays strict on purpose, because different text would mean two code paths disagree.
- **Test now.** The case "a number is printed in another format" expects exactly this hash violation.

## 3. 19 rows lost their asset type when the amount had no upper end

- **Found by:** the first full live run (Phase 6), in the `rejected` records: 19 rows with reason "no asset type code".
- **Symptom.** Assets such as `U.S Treasury Bills [GS] $1,000,000`: the amount had been glued to the asset name.
- **Cause.** The parser only joined a wrapped amount when the first line ended with `-` (`$15,001 -` / `$50,000`). These rows say `Spouse/DC Over` on the first line and `$1,000,000` on the next.
- **Fix.** `_amount_is_open` in `pipeline/ptr.py`: an amount is unfinished if it ends with `-` or `Over`, or has no number yet.
- **Test now.** `test_open_ended_amount_on_the_next_line` in `pipeline/tests/test_ptr.py`. After the fix the full run has zero rows with that reason.
- **Why it was easy to find.** Every dropped row is written as a `rejected` record with a reason, so the bug showed up as a line in a count, not as silently missing data.
