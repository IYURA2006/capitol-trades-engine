"""The ctx object a task receives.

Everything a task may read goes through ctx, so the same task function
runs unchanged under the engine and under run_local.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Protocol


class BlobStore:
    """Files named by their SHA-256 (design rule 8).

    Writing the same content twice gives the same name, so a repeated
    write is harmless. A write goes to a temp file first and is renamed
    at the end, so a worker killed half-way never leaves a broken blob.
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if path.exists():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on the same filesystem
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def get(self, sha256: str) -> bytes:
        data = self._path(sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise OSError(f"blob {sha256} is corrupt")
        return data

    def has(self, sha256: str) -> bool:
        return self._path(sha256).exists()


class RecordReader(Protocol):
    def read(self, run_id: str, kind: str) -> list[tuple[str, Any]]: ...

    def get(self, run_id: str, kind: str, key: str) -> Any: ...


class PostgresRecords:
    """Reads records with the read-only role. Workers cannot write (rule 4)."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None

    def read(self, run_id: str, kind: str) -> list[tuple[str, Any]]:
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        # COLLATE "C" sorts by code point, the same order as Python's sorted().
        rows = self._conn.execute(
            'SELECT key, data FROM records WHERE run_id = %s AND kind = %s ORDER BY key COLLATE "C"',
            (run_id, kind),
        ).fetchall()
        return [(key, data) for key, data in rows]

    def get(self, run_id: str, kind: str, key: str) -> Any:
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        row = self._conn.execute(
            "SELECT data FROM records WHERE run_id = %s AND kind = %s AND key = %s", (run_id, kind, key)
        ).fetchone()
        return None if row is None else row[0]


class MemoryRecords:
    """Records kept in a dict; used by run_local."""

    def __init__(self):
        self.data: dict[tuple[str, str], Any] = {}

    def read(self, run_id: str, kind: str) -> list[tuple[str, Any]]:
        return sorted((key, value) for (k, key), value in self.data.items() if k == kind)

    def get(self, run_id: str, kind: str, key: str) -> Any:
        return self.data.get((kind, key))


def task_rng(seed: int, task_type: str, key: str) -> random.Random:
    """A random generator that depends only on the run seed and the task."""
    digest = hashlib.sha256(f"{seed}\n{task_type}\n{key}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class Context:
    def __init__(self, *, run_id: str, task_type: str, key: str, attempt: int, seed: int,
                 snapshot_id: str, blobs: BlobStore, records: RecordReader, http: Any = None):
        self.run_id = run_id
        self.task_type = task_type
        self.key = key
        self.attempt = attempt  # never put this in an output (determinism rules)
        self.seed = seed
        self.snapshot_id = snapshot_id
        self.blobs = blobs
        self.http = http
        self.rng = task_rng(seed, task_type, key)
        self._records = records

    def records(self, kind: str) -> list[tuple[str, Any]]:
        """All records of one kind in this run as (key, data), sorted by key."""
        return self._records.read(self.run_id, kind)

    def record(self, kind: str, key: str) -> Any:
        """One record's data, or None if no task wrote it."""
        return self._records.get(self.run_id, kind, key)


def loads(payload: bytes | str | None) -> Any:
    if not payload:
        return {}
    return json.loads(payload)
