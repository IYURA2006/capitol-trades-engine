"""Research checks on a finished run. Reads files only; same input, same output.

Input: the records of a run (JSON from `capitol_sdk.cli local --out` or from
the chaos harness), the price files it points to, and the frozen reference
data in snapshots/research/. Output: one JSON file that tools/make_report.py
turns into the report.

Usage:
    python tools/run_research.py --json chaos/results/<run>.reference.json \
        --blobs snapshots/full/blobs --out out/research.json

Fixed before looking at results (see docs/decisions/007):
    main question window  250 trading days from the trade date
    copycat               hold 60 trading days from the disclosure date
Other windows are printed as a check on how much the answer depends on them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from bisect import bisect_right
from collections import Counter, defaultdict

import numpy as np

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from pipeline.finance import event_study as es
from pipeline.research import committee, copycat, members, random_picker, returns, splits

SEED = 7
MAIN_WINDOW = 250
WINDOWS = (20, 60, 120, 250)
COPYCAT_HOLD, COPYCAT_OTHER = 60, (20, 250)
REACTION_WINDOWS = (1, 2, 5)
MEMBER_MIN_BUYS = 20  # a member needs this many purchases for a percentile of their own
DRAWS = 1000


def load_json(*parts: str) -> dict:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return json.load(f)


def load_inputs(args) -> dict:
    records = load_json(args.json)["records"]
    series = {}
    for record in records:
        if record["kind"] == "price_series":
            sha = record["data"]["sha256"]
            with open(os.path.join(args.blobs, sha[:2], sha), "rb") as f:
                series[record["key"]] = es.split(json.loads(f.read())["prices"])
    research = args.reference
    funds = {"SPY": dict(zip(*series["SPY"], strict=True))}
    for name, fund in load_json(research, "benchmarks.json")["funds"].items():
        funds[name] = {day: close for day, close in fund["prices"]}
    return {
        "trades": [r["data"] for r in records if r["kind"] == "trade"],  # already sorted by key
        "series": series, "funds": funds,
        "sectors": load_json(research, "sectors.json")["tickers"],
        "committees": load_json(research, "committees.json"),
        "members": load_json(research, "members.json")["members"],
        "mapping": load_json(ROOT, "pipeline", "data", "committee_industries.json"),
    }


def enrich(data: dict) -> tuple[list[dict], dict]:
    """Add member id, committees and stock labels to every trade that has prices."""
    out, linked, name_differs = [], 0, 0
    seat_cache: dict = {}
    for trade in data["trades"]:
        if trade["ticker"] not in data["series"]:
            continue
        key = (trade["state_district"], trade["disclosure_date"])
        if key not in seat_cache:
            seat_cache[key] = members.find_member(data["members"], *key)
        member_id = seat_cache[key]
        if member_id and data["members"][member_id]["last"].casefold() not in trade["filer"].casefold():
            name_differs += 1  # the seat's member has another name: do not trust the link
            member_id = None
        linked += member_id is not None
        info = data["sectors"].get(trade["ticker"], {})
        out.append({
            **trade,
            # "sector_name" is the label; "sector" is kept free for the sector fund's return.
            # Trades of one person stay together even when the link failed.
            "member": member_id or f"{trade['filer']}|{trade['state_district']}",
            "member_id": member_id,
            "committees": members.committees_of(data["committees"], member_id, trade["trade_date"]) if member_id else set(),
            "sector_name": info.get("sector", ""), "industry": info.get("industry", ""),
            "company_state": info.get("state", "") if info.get("country") == "United States" else "",
            "market_cap": info.get("market_cap", 0),
        })
    counts = {"trades_with_prices": len(out), "linked_to_a_member": linked, "name_differs": name_differs,
              "people": len({t["member"] for t in out}),
              "buys": sum(t["side"] == "buy" for t in out), "sells": sum(t["side"] == "sell" for t in out)}
    return out, counts


def rows(trades: list[dict], data: dict, start: str, window: int, from_event_close: bool = False) -> list[dict]:
    """One row per trade that has prices for the whole window."""
    out = []
    for trade in trades:
        measured = returns.measure(data["series"][trade["ticker"]], data["funds"], trade["sector_name"],
                                   trade[f"{start}_date"], window, from_event_close)
        if measured is not None:
            out.append({**trade, **measured})
    return out


def rng_for(*labels) -> np.random.Generator:
    """An own random stream per table cell, so adding a table does not move the others."""
    import hashlib

    digest = hashlib.sha256("/".join(str(x) for x in (SEED, *labels)).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def overall(trades: list[dict], data: dict) -> dict:
    out: dict = {}
    for side in ("buy", "sell"):
        chosen = [t for t in trades if t["side"] == side]
        for start in ("trade", "disclosure"):
            for window in WINDOWS:
                measured = rows(chosen, data, start, window)
                for bench in returns.BENCHMARKS:
                    out[f"{side}/{start}/{window}/{bench}"] = returns.summarize(
                        measured, bench, rng_for("overall", side, start, window, bench))
    return out


def ai_split(buys: list[dict], data: dict) -> dict:
    out: dict = {"share_of_buys": float(np.mean([splits.is_ai_name(t["ticker"]) for t in buys])),
                 "most_bought": Counter(t["ticker"] for t in buys).most_common(10)}
    for window in (60, MAIN_WINDOW):
        measured = rows(buys, data, "trade", window)
        for label, keep in (("ai", True), ("rest", False)):
            part = [r for r in measured if splits.is_ai_name(r["ticker"]) == keep]
            for bench in ("SPY", "RSP"):
                out[f"{label}/{window}/{bench}"] = returns.summarize(part, bench, rng_for("ai", label, window, bench))
    return out


def by_sector(buys: list[dict], data: dict) -> dict:
    measured = rows(buys, data, "trade", MAIN_WINDOW)
    by: dict = defaultdict(list)
    for row in measured:
        by[row["sector_name"] or "unknown"].append(row)
    middle = defaultdict(float)
    for trade in buys:
        middle[trade["sector_name"] or "unknown"] += (trade["amount_low"] + trade["amount_high"]) / 2
    total = sum(middle.values())
    return {sector: {"share_of_dollars": middle[sector] / total,
                     "SPY": returns.summarize(part, "SPY", rng_for("sector", sector, "SPY")),
                     "sector": returns.summarize(part, "sector", rng_for("sector", sector, "own"))}
            for sector, part in sorted(by.items(), key=lambda kv: -len(kv[1]))}


def committee_match(trades: list[dict], data: dict) -> dict:
    out: dict = {}
    for group in ("narrow", "broad"):
        mapping = data["mapping"][group] if group == "narrow" else {**data["mapping"]["narrow"], **data["mapping"]["broad"]}
        for side in ("buy", "sell"):
            chosen = [t for t in trades if t["side"] == side and t["member_id"]]
            for t in chosen:
                t["_class"] = committee.classify(mapping, t["committees"], t["sector_name"], t["industry"])
            counts = Counter(t["_class"] for t in chosen)
            out[f"{group}/{side}/counts"] = {"with_member": len(chosen), **counts,
                                            "members_matched": len({t["member"] for t in chosen if t["_class"] == "matched"})}
            for window in (60, MAIN_WINDOW):
                measured = [r for r in rows(chosen, data, "trade", window) if r["_class"] != "other"]
                for r in measured:
                    r["matched"] = r["_class"] == "matched"
                for bench in ("sector", "SPY"):
                    out[f"{group}/{side}/{window}/{bench}"] = returns.difference(
                        measured, "matched", bench, rng_for("committee", group, side, window, bench))
    return out


def copycat_portfolio(buys: list[dict], data: dict) -> dict:
    positions = [(t["ticker"], t["disclosure_date"]) for t in buys]
    out = {}
    for hold in (COPYCAT_HOLD, *COPYCAT_OTHER):
        portfolio = copycat.daily_returns(positions, data["series"], hold)
        for bench in ("SPY", "RSP"):
            result = copycat.compare(portfolio, data["funds"][bench], rng_for("copycat", hold, bench))
            if hold != COPYCAT_HOLD:
                result.pop("curve", None)  # only the main one is drawn
            out[f"{hold}/{bench}"] = result
    return out


def random_test(buys: list[dict], data: dict) -> dict:
    by_sector_tickers: dict = defaultdict(list)
    for ticker, info in sorted(data["sectors"].items()):
        if info.get("sector") and ticker in data["series"] and ticker != "SPY":
            by_sector_tickers[info["sector"]].append(ticker)
    out: dict = {"universe": sum(len(v) for v in by_sector_tickers.values())}
    cache: dict = {}

    def candidates_for(sector: str, day: str, window: int):
        key = (sector, day, window)
        if key not in cache:
            found, caps = [], []
            for ticker in by_sector_tickers[sector]:
                value = es.window_return(data["series"][ticker], day, window)
                if value is not None:
                    found.append(value[2])
                    # Size on the day of the purchase, not today's: today's size
                    # scaled back by the price change since then. Using today's
                    # size would favour the stocks that rose afterwards.
                    dates, closes = data["series"][ticker]
                    then = closes[bisect_right(dates, day)] / closes[-1] if closes[-1] > 0 else 0.0
                    caps.append(float(data["sectors"][ticker].get("market_cap") or 0) * then)
            cache[key] = (np.array(found), np.array(caps))
        return cache[key]

    for window in (60, MAIN_WINDOW):
        measured = [r for r in rows(buys, data, "trade", window) if r["sector_name"]]
        pools = [candidates_for(r["sector_name"], r["trade_date"], window) for r in measured]
        real = [r["stock"] for r in measured]
        for weighting in ("equal", "by_size"):
            cands = [(found, None if weighting == "equal" else caps + 1.0) for found, caps in pools]
            result = random_picker.run(real, cands, rng_for("random", window, weighting), DRAWS)
            means = result.pop("random_means")
            if window == MAIN_WINDOW and weighting == "by_size":
                counts, edges = np.histogram(means, bins=30)
                result["histogram"] = {"counts": counts.tolist(), "edges": [float(e) for e in edges]}
            out[f"{window}/{weighting}"] = result

        if window == 60:
            # One percentile per member with enough purchases. Under pure luck
            # these are spread evenly between 0 and 1. No names are kept.
            per_member: dict = defaultdict(list)
            for r, pool in zip(measured, pools, strict=True):
                per_member[r["member"]].append((r["stock"], (pool[0], pool[1] + 1.0)))
            percentiles = []
            for i, member in enumerate(sorted(per_member)):
                picks = per_member[member]
                if len(picks) >= MEMBER_MIN_BUYS:
                    result = random_picker.run([p[0] for p in picks], [p[1] for p in picks],
                                               rng_for("random-member", i), DRAWS)
                    percentiles.append(round(result["percentile"], 3))
            out["members"] = {"min_buys": MEMBER_MIN_BUYS, "window": 60, "percentiles": sorted(percentiles),
                              "above_95": sum(p > 0.95 for p in percentiles),
                              "below_05": sum(p < 0.05 for p in percentiles)}
    return out


def reaction(trades: list[dict], data: dict) -> dict:
    out = {}
    for side in ("buy", "sell"):
        chosen = [t for t in trades if t["side"] == side]
        for window in REACTION_WINDOWS:
            measured = rows(chosen, data, "disclosure", window, from_event_close=True)
            for bench in ("SPY", "sector"):
                out[f"{side}/{window}/{bench}"] = returns.summarize(measured, bench, rng_for("reaction", side, window, bench))
    return out


def late_filers(buys: list[dict], data: dict) -> dict:
    delays = [splits.days_to_disclosure(t) for t in buys]
    out: dict = {"share_late": float(np.mean([d > splits.LATE_AFTER_DAYS for d in delays])),
                 "median_days": float(np.median(delays))}
    for window in (60, MAIN_WINDOW):
        measured = rows(buys, data, "trade", window)
        for r in measured:
            r["late"] = splits.is_late(r)
        for bench in ("SPY", "sector"):
            out[f"{window}/{bench}"] = returns.difference(measured, "late", bench, rng_for("late", window, bench))
    return out


def home_state(buys: list[dict], data: dict) -> dict:
    known = [t for t in buys if splits.is_home_state(t, t["company_state"]) is not None]
    out: dict = {"buys_with_known_state": len(known),
                 "share_home": float(np.mean([splits.is_home_state(t, t["company_state"]) for t in known])) if known else None}
    for window in (60, MAIN_WINDOW):
        measured = rows(known, data, "trade", window)
        for r in measured:
            r["home"] = splits.is_home_state(r, r["company_state"])
        for bench in ("SPY", "sector"):
            out[f"{window}/{bench}"] = returns.difference(measured, "home", bench, rng_for("home", window, bench))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True)
    parser.add_argument("--blobs", default=os.path.join(ROOT, "snapshots", "full", "blobs"))
    parser.add_argument("--reference", default=os.path.join(ROOT, "snapshots", "research"),
                        help="folder with the frozen reference data")
    parser.add_argument("--out", default=os.path.join(ROOT, "out", "research.json"))
    args = parser.parse_args()

    data = load_inputs(args)
    trades, counts = enrich(data)
    buys = [t for t in trades if t["side"] == "buy"]
    result = {
        "settings": {"seed": SEED, "main_window": MAIN_WINDOW, "windows": WINDOWS, "copycat_hold": COPYCAT_HOLD,
                     "draws": DRAWS, "late_after_days": splits.LATE_AFTER_DAYS},
        "counts": counts,
        "overall": overall(trades, data),
        "ai": ai_split(buys, data),
        "sectors": by_sector(buys, data),
        "committee": committee_match(trades, data),
        "copycat": copycat_portfolio(buys, data),
        "random": random_test(buys, data),
        "reaction": reaction(trades, data),
        "late": late_filers(buys, data),
        "home": home_state(buys, data),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, sort_keys=True, indent=1)
        f.write("\n")
    print(f"research written to {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
