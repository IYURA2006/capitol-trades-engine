"""The real pipeline, part 2: prices and the event study.

    normalize_trades ──> fetch_prices (one per ticker, plus the benchmark)
                    └──> event_study       (join, tolerates tickers without prices)
                    └──> aggregate_results (means and bootstrap intervals)
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta

import numpy as np

from capitol_sdk import Fatal, Record, Result, TaskSpec, task

from ..finance import event_study as es
from ..finance import prices as price_sources

PRICE_RATE_KEY = "prices"
RESAMPLES = 2000


def plan(records: list[Record]) -> list[TaskSpec]:
    """The sub-DAG that normalize_trades emits."""
    trades = [r.data for r in records if r.kind == "trade"]
    if not trades:
        return []
    # A fixed date range taken from the data, never from today's date (rule 7).
    start = (date.fromisoformat(min(t["trade_date"] for t in trades)) - timedelta(days=7)).isoformat()
    end = (date.fromisoformat(max(t["disclosure_date"] for t in trades)) + timedelta(days=100)).isoformat()
    tickers = sorted({t["ticker"] for t in trades} | {price_sources.BENCHMARK})

    fetches = [TaskSpec("fetch_prices", ticker, {"ticker": ticker, "start": start, "end": end},
                        rate_key=PRICE_RATE_KEY) for ticker in tickers]
    study = TaskSpec("event_study", "all", {}, depends_on=[f.ref for f in fetches], tolerate_failures=True)
    aggregate = TaskSpec("aggregate_results", "all", {"resamples": RESAMPLES}, depends_on=[study.ref])
    return [*fetches, study, aggregate]


def price_key(ticker: str, start: str, end: str) -> str:
    return f"prices://{ticker}?start={start}&end={end}"


@task("fetch_prices", timeout_s=120)
def fetch_prices(ctx, payload):
    ticker, start, end = payload["ticker"], payload["start"], payload["end"]
    # Snapshot mode: read the frozen bytes. Live mode: download and keep them.
    data = ctx.http.fetch(price_key(ticker, start, end), lambda: price_sources.download(ticker, start, end))
    series = json.loads(data)
    if not series["prices"]:
        raise Fatal(f"no prices for {ticker}")
    # The series is big, so it goes to the blob store; only its hash travels
    # through the coordinator.
    sha = ctx.blobs.put(data)
    return Result(records=[Record("price_series", ticker, {
        "sha256": sha, "days": len(series["prices"]), "source": series["source"],
        "first": series["prices"][0][0], "last": series["prices"][-1][0]})])


def load_series(ctx, ticker: str) -> tuple[list[str], list[float]] | None:
    info = ctx.record("price_series", ticker)
    if info is None:
        return None
    return es.split(json.loads(ctx.blobs.get(info["sha256"]))["prices"])


@task("event_study", timeout_s=600)
def event_study(ctx, payload):
    bench_series = load_series(ctx, price_sources.BENCHMARK)
    if bench_series is None:
        raise Fatal("no benchmark prices; nothing can be measured")
    bench = dict(zip(*bench_series, strict=True))

    cache: dict[str, tuple[list[str], list[float]] | None] = {}
    records: list[Record] = []
    skipped: Counter[str] = Counter()
    for key, trade in ctx.records("trade"):  # sorted by key
        ticker = trade["ticker"]
        if ticker not in cache:
            cache[ticker] = load_series(ctx, ticker)
        if cache[ticker] is None:
            skipped["no prices for ticker"] += 1
            continue
        study = es.study_trade(trade, cache[ticker], bench)
        if all(v is None for per_start in study.values() for v in per_start.values()):
            skipped["no prices around the dates"] += 1
            continue
        records.append(Record("event_result", key, {
            "ticker": ticker, "side": trade["side"],
            "amount_low": trade["amount_low"], "amount_high": trade["amount_high"],
            "ar": study}))

    summary = {"trades": len(records) + sum(skipped.values()), "studied": len(records),
               "skipped": dict(sorted(skipped.items())),
               "tickers_with_prices": sum(1 for v in cache.values() if v is not None),
               "tickers_without_prices": sorted(t for t, v in cache.items() if v is None)}
    records.append(Record("study_summary", "all", summary))
    return Result(output={"studied": summary["studied"]}, records=records)


@task("aggregate_results", timeout_s=600)
def aggregate_results(ctx, payload):
    results = [(data, data["ar"]) for _, data in ctx.records("event_result")]  # sorted by key
    # All randomness comes from ctx.rng, which depends only on seed and task.
    rng = np.random.default_rng(ctx.rng.getrandbits(64))
    rows = es.aggregate(results, rng, payload["resamples"])
    return Result(records=[
        Record("aggregate", f"{row['start']}-{row['window']:02d}-{row['weight']}", row) for row in rows])
