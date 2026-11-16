"""The real pipeline, part 1: from the House index to clean trades.

    discover_filings ──> fetch_document (one per filing, rate-limited)
                    └──> parse_filing   (one per filing, after its fetch)
                    └──> normalize_trades (one join, tolerates failed parses)

Every task is a pure function of its payload and the snapshot (rule 7).
"""
from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path

from capitol_sdk import Fatal, Record, Result, TaskSpec, task, workflow

from .. import ptr
from . import study  # also registers the price and study tasks with the worker

RATE_KEY = "house-clerk"
TICKER_MAP = Path(__file__).resolve().parent.parent / "data" / "ticker_map.json"


@workflow("capitol")
def capitol(params: dict) -> list[TaskSpec]:
    years = params.get("years", [2024, 2025])
    payload = {"years": [int(y) for y in years], "limit": int(params.get("limit", 0))}
    return [TaskSpec("discover_filings", "all", payload, rate_key=RATE_KEY)]


# The 20 filings read by hand in Phase 0 (docs/data-notes.md). They are the
# committed sample snapshot: 19 digital filings and one scanned one.
SAMPLE_DOCS = [
    "20018054", "20024249", "20024517", "20024842", "20025257", "20025439", "20025519",
    "20025708", "20025787", "20025848", "20025892", "20026125", "20026382", "20026428",
    "20026726", "20027846", "20029084", "20031007", "20033581", "8221237",
]


@workflow("capitol_sample")
def capitol_sample(params: dict) -> list[TaskSpec]:
    payload = {"years": [2024, 2025], "limit": 0, "doc_ids": SAMPLE_DOCS}
    return [TaskSpec("discover_filings", "all", payload, rate_key=RATE_KEY)]


@task("discover_filings", timeout_s=300)
def discover_filings(ctx, payload):
    filings = []
    for year in payload["years"]:
        filings += ptr.parse_index(ctx.http.get(ptr.INDEX_URL.format(year=year)), year)
    filings.sort(key=lambda f: f["doc_id"])
    if payload.get("doc_ids"):
        wanted = set(payload["doc_ids"])
        filings = [f for f in filings if f["doc_id"] in wanted]
    if payload.get("limit"):
        filings = filings[: payload["limit"]]  # small runs for demos and tests

    sub_dag, parses = [], []
    for f in filings:
        fetch = TaskSpec("fetch_document", f["doc_id"],
                         {"doc_id": f["doc_id"], "url": ptr.PDF_URL.format(year=f["year"], doc_id=f["doc_id"])},
                         rate_key=RATE_KEY)
        parse = TaskSpec("parse_filing", f["doc_id"], {"doc_id": f["doc_id"]}, depends_on=[fetch.ref])
        sub_dag += [fetch, parse]
        parses.append(parse.ref)
    # One scanned PDF must not stop the pipeline, so the join tolerates failures.
    sub_dag.append(TaskSpec("normalize_trades", "all", {}, depends_on=parses, tolerate_failures=True))

    return Result(
        output={"filings": len(filings)},
        records=[Record("filing", f["doc_id"], f) for f in filings],
        sub_dag=sub_dag,
    )


@task("fetch_document", timeout_s=120)
def fetch_document(ctx, payload):
    data = ctx.http.get(payload["url"])
    sha = ctx.blobs.put(data)  # content-addressed: a second write is harmless (rule 8)
    return Result(records=[Record("document", payload["doc_id"], {"sha256": sha, "bytes": len(data)})])


def pdf_text(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


@task("parse_filing", timeout_s=180)
def parse_filing(ctx, payload):
    document = ctx.record("document", payload["doc_id"])
    if document is None:
        raise Fatal("no document record; the fetch did not succeed")
    text = pdf_text(ctx.blobs.get(document["sha256"]))
    if len(text.replace("\x00", "").strip()) < 200:
        raise Fatal("scanned")  # no text layer; this project has no OCR
    rows = ptr.parse_ptr_text(text)
    return Result(records=[Record("raw_trades", payload["doc_id"], {"rows": rows})])


@task("normalize_trades", timeout_s=300)
def normalize_trades(ctx, payload):
    ticker_map = {k.lower(): v for k, v in json.loads(TICKER_MAP.read_text()).items()}
    filings = dict(ctx.records("filing"))
    parsed = ctx.records("raw_trades")  # sorted by key, so the output order is fixed

    records: list[Record] = []
    reasons: Counter[str] = Counter()
    trades = 0
    for doc_id, data in parsed:
        for i, row in enumerate(data["rows"]):
            key = f"{doc_id}-{i:03d}"
            trade, reason = ptr.normalize_row(row, filings[doc_id], ticker_map)
            if trade is None:
                reasons[reason] += 1
                records.append(Record("rejected", key, {"reason": reason, "asset": row["asset"], "type": row["type"]}))
            else:
                trades += 1
                records.append(Record("trade", key, trade))

    summary = {
        "filings_found": len(filings),
        "filings_parsed": len(parsed),
        "filings_skipped": len(filings) - len(parsed),  # scanned, or the fetch failed
        "filings_without_rows": sum(1 for _, d in parsed if not d["rows"]),
        "rows": trades + sum(reasons.values()),
        "trades": trades,
        "rejected": dict(sorted(reasons.items())),
    }
    records.append(Record("trades_summary", "all", summary))
    return Result(output=summary, records=records, sub_dag=next_stage(ctx, records))


def next_stage(ctx, records: list[Record]) -> list[TaskSpec]:
    """Prices and the event study: see study.py."""
    return study.plan(records)
