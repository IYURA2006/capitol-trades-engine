"""Freeze everything a live run downloaded into snapshots/<id>/.

A live run leaves one small note per request in <blob_dir>/_http/ ("this
URL gave this blob"). This tool copies the notes into one manifest and the
blobs next to it. From then on, runs with --snapshot <id> read only this
folder and can be repeated exactly.

Usage:
    python tools/make_snapshot.py full                       # everything captured
    python tools/make_snapshot.py sample --docs 20025848,... # a small one to commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sdk"), str(ROOT)]

from capitol_sdk.context import BlobStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot_id")
    parser.add_argument("--blob-dir", default=str(ROOT / "blobs"))
    parser.add_argument("--out", default=str(ROOT / "snapshots"))
    parser.add_argument("--docs", default="", help="keep only these PTR doc ids (and all non-PDF entries)")
    parser.add_argument("--prices-for", default="", help="JSON of a local run; keep only its price series")
    args = parser.parse_args()

    source = BlobStore(args.blob_dir)
    notes = sorted((Path(args.blob_dir) / "_http").glob("[0-9a-f]*"))
    if not notes:
        print(f"no captured requests in {args.blob_dir}/_http; run the pipeline in live mode first")
        return 1
    keep_docs = {d for d in args.docs.split(",") if d}
    keep_prices: set[str] | None = None
    if args.prices_for:
        # Keep only the price series that this (smaller) run asked for.
        from capitol_sdk import Record
        from pipeline.tasks import study

        with open(args.prices_for, encoding="utf-8") as f:
            records = [Record(r["kind"], r["key"], r["data"]) for r in json.load(f)["records"]]
        keep_prices = {study.price_key(**t.payload) for t in study.plan(records) if t.type == "fetch_prices"}

    target_dir = Path(args.out) / args.snapshot_id
    target = BlobStore(target_dir / "blobs")
    entries: dict[str, str] = {}
    total = 0
    for note_path in notes:
        note = json.loads(note_path.read_text())
        key = note["key"]
        if keep_docs and "/ptr-pdfs/" in key and key.rsplit("/", 1)[-1].removesuffix(".pdf") not in keep_docs:
            continue
        if keep_prices is not None and key.startswith("prices://") and key not in keep_prices:
            continue
        data = source.get(note["sha256"])  # also verifies the hash
        target.put(data)
        entries[key] = note["sha256"]
        total += len(data)

    manifest = {
        "id": args.snapshot_id,
        "entries": dict(sorted(entries.items())),
    }
    text = json.dumps(manifest, indent=1, sort_keys=True)
    (target_dir / "manifest.json").write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    print(f"snapshot {args.snapshot_id}: {len(entries)} entries, {total / 1e6:.1f} MB, manifest sha256 {digest[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
