"""ctx.http: the only door from a task to the outside world.

Snapshot mode (chaos and reference runs): every answer comes from a frozen
folder, so a run can be repeated exactly and no website is touched.

Live mode (used once, to build a snapshot): the request goes to the network,
the answer is stored in the blob store, and a small note "this URL gave this
blob" is written next to it. `tools/make_snapshot.py` turns those notes into
a snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from .context import BlobStore
from .types import Fatal, RetryAfter

USER_AGENT = "research-script/0.1 (non-commercial student project; 1 request per 2s)"
MIN_GAP_S = 2.0  # politeness inside one process; the engine's rate key covers many workers


class SnapshotHttp:
    """Answers from snapshots/<id>/manifest.json and snapshots/<id>/blobs/."""

    def __init__(self, snapshot_dir: str | os.PathLike, snapshot_id: str):
        root = Path(snapshot_dir) / snapshot_id
        manifest = root / "manifest.json"
        if not manifest.exists():
            raise Fatal(f"snapshot {snapshot_id!r} not found at {root}")
        self._index: dict[str, str] = json.loads(manifest.read_text())["entries"]
        self._blobs = BlobStore(root / "blobs")

    def get(self, url: str) -> bytes:
        return self.fetch(url, None)

    def fetch(self, key: str, fetcher: Callable[[], bytes] | None) -> bytes:
        sha = self._index.get(key)
        if sha is None:
            # In a frozen world a missing entry is permanent, so do not retry.
            raise Fatal(f"not in snapshot: {key}")
        return self._blobs.get(sha)


class LiveHttp:
    """Real requests, slow and polite, with every answer kept."""

    _lock = threading.Lock()
    _last_request: ClassVar[dict[str, float]] = {}  # shared by all instances in the process

    def __init__(self, blob_dir: str | os.PathLike):
        self._blobs = BlobStore(blob_dir)
        self._notes = Path(blob_dir) / "_http"

    def get(self, url: str) -> bytes:
        return self.fetch(url, lambda: self._download(url))

    def fetch(self, key: str, fetcher: Callable[[], bytes] | None) -> bytes:
        """Return the bytes for key. An answer already captured is reused, so a
        retried task does not hit the website a second time."""
        note = self._notes / hashlib.sha256(key.encode()).hexdigest()
        if note.exists():
            return self._blobs.get(json.loads(note.read_text())["sha256"])
        if fetcher is None:
            raise Fatal(f"no way to fetch {key}")
        data = fetcher()
        sha = self._blobs.put(data)
        self._notes.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._notes, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump({"key": key, "sha256": sha, "bytes": len(data)}, f)
        os.replace(tmp, note)
        return data

    def _download(self, url: str) -> bytes:
        import requests

        host = urlparse(url).netloc
        with self._lock:
            wait = self._last_request.get(host, 0) + MIN_GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After", "")
            raise RetryAfter(float(retry_after) if retry_after.isdigit() else 30, f"HTTP {response.status_code} for {url}")
        if response.status_code in (403, 404, 410):
            raise Fatal(f"HTTP {response.status_code} for {url}")
        response.raise_for_status()
        return response.content


def make_http(snapshot_id: str, settings: dict):
    if snapshot_id:
        return SnapshotHttp(settings.get("snapshot_dir", "snapshots"), snapshot_id)
    return LiveHttp(settings.get("blob_dir", "blobs"))
