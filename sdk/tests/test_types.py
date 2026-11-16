import pytest

from capitol_sdk import canonical_json, task_id
from capitol_sdk.context import BlobStore, task_rng


def test_task_id_matches_go():
    # Same constant as coordinator/internal/engine/dag_test.go.
    assert task_id("run-1", "parse_filing", "20025848") == (
        "fba29246979c57810e9abd48918777b4565ff730fbd6a5ad85cbd8cc6db09229")


def test_canonical_json_sorts_keys_and_rounds_floats():
    a = canonical_json({"b": 0.1 + 0.2, "a": [1, 2.0, None, True]})
    b = canonical_json({"a": [1, 2.0, None, True], "b": 0.3})
    assert a == b == b'{"a":[1,2.0,null,true],"b":0.3}'


def test_canonical_json_has_no_negative_zero():
    assert canonical_json(-0.00000000001) == b"0.0"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "x"}, object()])
def test_canonical_json_rejects_values_that_cannot_be_compared(bad):
    with pytest.raises((ValueError, TypeError)):
        canonical_json(bad)


def test_rng_depends_only_on_seed_and_task():
    assert task_rng(7, "t", "k").random() == task_rng(7, "t", "k").random()
    assert task_rng(7, "t", "k").random() != task_rng(8, "t", "k").random()
    assert task_rng(7, "t", "k").random() != task_rng(7, "t", "other").random()


def test_blob_store_is_content_addressed(tmp_path):
    blobs = BlobStore(tmp_path)
    sha = blobs.put(b"hello")
    assert sha == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert blobs.put(b"hello") == sha  # second write is harmless
    assert blobs.get(sha) == b"hello"
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [sha]  # no temp files left


def test_blob_store_detects_corruption(tmp_path):
    blobs = BlobStore(tmp_path)
    sha = blobs.put(b"hello")
    (tmp_path / sha[:2] / sha).write_bytes(b"half a fi")
    with pytest.raises(IOError):
        blobs.get(sha)
