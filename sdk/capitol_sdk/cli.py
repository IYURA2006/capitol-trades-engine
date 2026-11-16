"""Command line for the SDK.

    python -m capitol_sdk.cli submit --workflow toy --run r1 --seed 7 [--param n=2000]
    python -m capitol_sdk.cli local  --workflow toy --seed 7 --out reference.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import engine_pb2 as pb
from .registry import first_tasks, load_modules
from .runner import spec_to_dict


def _params(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        name, _, raw = pair.partition("=")
        try:
            out[name] = json.loads(raw)
        except json.JSONDecodeError:
            out[name] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capitol_sdk.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "local"):
        p = sub.add_parser(name)
        p.add_argument("--workflow", required=True)
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--snapshot", default="")
        p.add_argument("--param", action="append", default=[], help="name=value, value parsed as JSON")
        p.add_argument("--modules", default=os.environ.get("TASK_MODULES", "pipeline.tasks.toy"))
        if name == "submit":
            p.add_argument("--run", required=True)
            p.add_argument("--coordinators", default=os.environ.get("COORDINATORS", "localhost:50051,localhost:50052"))
        else:
            p.add_argument("--out", default="-")
            p.add_argument("--blob-dir", default=os.environ.get("BLOB_DIR", "blobs"))
    args = parser.parse_args(argv)
    load_modules(args.modules.split(","))
    params = _params(args.param)

    if args.command == "submit":
        from .client import EngineClient

        specs = [spec_to_dict(s) for s in first_tasks(args.workflow, params)]
        request = pb.SubmitRunRequest(
            run_id=args.run, workflow=args.workflow, snapshot_id=args.snapshot, seed=args.seed,
            tasks=[pb.TaskSpec(type=s["type"], key=s["key"], payload=s["payload"],
                               depends_on_keys=s["depends_on"], rate_key=s["rate_key"],
                               tolerate_failures=s["tolerate_failures"], max_attempts=s["max_attempts"])
                   for s in specs])
        reply = EngineClient(args.coordinators.split(",")).submit_run(request)
        print(f"run {args.run}: {'created' if reply.created else 'already exists'} ({len(specs)} first tasks)")
        return 0

    from .http import make_http
    from .local import run_local

    http = make_http(args.snapshot, {"snapshot_dir": os.environ.get("SNAPSHOT_DIR", "snapshots"),
                                     "blob_dir": args.blob_dir})
    result = run_local(args.workflow, args.snapshot, args.seed, params, blob_dir=args.blob_dir, http=http)
    text = result.to_json()
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{len(result.tasks)} tasks, {len(result.records)} records, run {result.run_state} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
