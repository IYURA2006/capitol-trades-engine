import hashlib

import pytest

import pipeline.tasks.toy  # noqa: F401  (registers the toy tasks)
from capitol_sdk import Record, Result, TaskSpec, run_local, task, workflow


def test_toy_workflow_gives_the_expected_hash(tmp_path):
    result = run_local("toy", seed=7, params={"n": 50, "max_sleep_ms": 0}, blob_dir=tmp_path)
    assert result.run_state == "SUCCEEDED"
    assert len(result.tasks) == 52 and set(result.tasks.values()) == {"SUCCEEDED"}

    expected = hashlib.sha256()
    for i in range(50):
        leaf = hashlib.sha256(f"7:{i}".encode()).hexdigest()
        expected.update(f"{i:06d}:{leaf}\n".encode())
    assert result.records["combined_hash\nall"] == {"count": 50, "sha256": expected.hexdigest()}


def test_same_seed_same_result_and_other_seed_differs(tmp_path):
    a = run_local("toy", seed=1, params={"n": 20, "max_sleep_ms": 0}, blob_dir=tmp_path).to_json()
    b = run_local("toy", seed=1, params={"n": 20, "max_sleep_ms": 0}, blob_dir=tmp_path).to_json()
    c = run_local("toy", seed=2, params={"n": 20, "max_sleep_ms": 0}, blob_dir=tmp_path).to_json()
    assert a == b
    assert a != c


def test_fatal_cancels_strict_child_and_tolerant_join_still_runs(tmp_path):
    result = run_local("toy_failures", params={"fail_first": 3}, blob_dir=tmp_path)
    assert result.tasks == {
        "after_fatal/1": "CANCELLED",
        "always_fatal/1": "DEAD_LETTER",
        "flaky/1": "SUCCEEDED",
        "tolerant_join/1": "SUCCEEDED",
    }
    assert result.run_state == "FAILED"
    assert "should_not_exist\n1" not in result.records
    assert result.records["join_done\n1"] == {"flaky": 1}


def test_task_that_always_fails_dead_letters_after_max_attempts(tmp_path):
    result = run_local("toy_failures", params={"fail_first": 99}, blob_dir=tmp_path)
    assert result.tasks["flaky/1"] == "DEAD_LETTER"
    assert result.records["join_done\n1"] == {"flaky": 0}


def test_unknown_dependency_is_an_error(tmp_path):
    @workflow("bad_dep")
    def bad_dep(params):
        return [TaskSpec("noop", "1", depends_on=["noop/missing"])]

    @task("noop")
    def noop(ctx, payload):
        return Result(records=[Record("x", ctx.key, {})])

    with pytest.raises(ValueError, match="unknown task"):
        run_local("bad_dep", blob_dir=tmp_path)
