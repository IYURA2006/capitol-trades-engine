"""Download the reference data for the research checks, once, and freeze it.

Writes small JSON files to snapshots/research/. The analysis reads only these
files, so it gives the same answer every time and needs no network.

    sectors.json     ticker -> sector, industry, headquarters state   (Yahoo)
    benchmarks.json  prices of the comparison funds                    (same source as the run)
    committees.json  House committee -> member ids, per Congress       (unitedstates/congress-legislators)
    members.json     member id -> House terms (state, district, dates) (same repo)

This is a one-time tool and the only place that needs PyYAML:
    .venv/bin/pip install pyyaml

Usage:
    python tools/fetch_research_data.py --json chaos/results/<run>.reference.json [--only sectors,...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))

OUT = os.path.join(os.path.dirname(__file__), "..", "snapshots", "research")
USER_AGENT = "research-script/0.1 (non-commercial student project)"
REPO = "unitedstates/congress-legislators"
RAW = "https://raw.githubusercontent.com/" + REPO + "/{sha}/{path}"
# The committee file only holds "now", so one old commit is pinned per Congress.
COMMITTEE_COMMITS = {
    "118": {"sha": "c22da268b57fe3a392a142674b78c5e7a21acb49", "as_of": "2024-06-28"},
    "119": {"sha": "65259e46abc0b11f9e643f06a021b1d5dd5dbdc2", "as_of": "2025-06-17"},
}
BENCHMARKS = ["RSP", "QQQ", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
STUDY_START, STUDY_END = "2023-01-03", "2026-12-31"  # House terms that touch this range are kept


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def write(name: str, data: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, indent=1)
        f.write("\n")
    print(f"wrote {path} ({os.path.getsize(path) // 1024} KB)", flush=True)


def run_tickers(path: str) -> tuple[list[str], str, str]:
    """Tickers that have prices in the run, and the date range of those prices."""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)["records"]
    series = {r["key"]: r["data"] for r in records if r["kind"] == "price_series"}
    return sorted(series), min(s["first"] for s in series.values()), max(s["last"] for s in series.values())


def fetch_sectors(tickers: list[str]) -> None:
    import logging

    import yfinance

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        for attempt in range(3):
            try:
                info = yfinance.Ticker(ticker).info
                out[ticker] = {"sector": info.get("sector") or "", "industry": info.get("industry") or "",
                               "state": info.get("state") or "", "country": info.get("country") or "",
                               "type": info.get("quoteType") or "", "market_cap": info.get("marketCap") or 0}
                break
            except Exception as err:  # noqa: BLE001 - an unofficial source; note it and go on
                print(f"  {ticker}: {type(err).__name__} (attempt {attempt + 1})", flush=True)
                time.sleep(5)
        else:
            out[ticker] = {"sector": "", "industry": "", "state": "", "country": "", "type": "", "market_cap": 0}
        if i % 100 == 0:
            print(f"  sectors {i}/{len(tickers)}", flush=True)
        time.sleep(0.4)
    write("sectors.json", {"source": "Yahoo Finance via yfinance (.info), labels as of the fetch date",
                           "fetched": date.today().isoformat(), "tickers": out})


def fetch_benchmarks(start: str, end: str) -> None:
    from datetime import timedelta

    from pipeline.finance import prices as price_sources

    # The end date of a download is not included, so ask for one day more.
    end_plus = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    out = {}
    for ticker in BENCHMARKS:
        series = json.loads(price_sources.download(ticker, start, end_plus))
        out[ticker] = {"source": series["source"], "prices": [[d, round(c, 6)] for d, c in series["prices"]]}
        print(f"  {ticker}: {len(series['prices'])} days", flush=True)
    write("benchmarks.json", {"start": start, "end": end, "fetched": date.today().isoformat(), "funds": out})


def fetch_congress() -> None:
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    main_sha = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/main"))["sha"]

    names = {}
    for c in yaml.load(get(RAW.format(sha=main_sha, path="committees-current.yaml")), Loader=loader):
        if c.get("type") == "house":
            names[c["thomas_id"]] = c["name"]

    congresses = {}
    for congress, pin in COMMITTEE_COMMITS.items():
        raw = yaml.load(get(RAW.format(sha=pin["sha"], path="committee-membership-current.yaml")), Loader=loader)
        # Four letters = a full committee ("HSAS"); longer ids are subcommittees.
        congresses[congress] = {
            "as_of": pin["as_of"], "commit": pin["sha"],
            "committees": {cid: sorted({m["bioguide"] for m in members})
                           for cid, members in sorted(raw.items()) if len(cid) == 4 and cid.startswith("HS")},
        }
        print(f"  congress {congress}: {len(congresses[congress]['committees'])} House committees", flush=True)
    write("committees.json", {"source": f"https://github.com/{REPO}", "names": names, "congresses": congresses})

    members = {}
    for path in ("legislators-current.yaml", "legislators-historical.yaml"):
        for person in yaml.load(get(RAW.format(sha=main_sha, path=path)), Loader=loader):
            terms = [{"start": t["start"], "end": t["end"], "state": t["state"], "district": int(t.get("district", 0))}
                     for t in person["terms"]
                     if t["type"] == "rep" and t["end"] >= STUDY_START and t["start"] <= STUDY_END]
            if terms:
                # The last name is kept only to check the match with the filing.
                members[person["id"]["bioguide"]] = {"last": person["name"]["last"], "terms": terms}
        print(f"  {path}: {len(members)} members so far", flush=True)
    write("members.json", {"source": f"https://github.com/{REPO}", "commit": main_sha, "members": members})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True, help="reference JSON of a finished run")
    parser.add_argument("--only", default="congress,benchmarks,sectors")
    args = parser.parse_args()
    tickers, start, end = run_tickers(args.json)
    steps = args.only.split(",")
    if "congress" in steps:
        fetch_congress()
    if "benchmarks" in steps:
        fetch_benchmarks(start, end)
    if "sectors" in steps:
        fetch_sectors(tickers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
