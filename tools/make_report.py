"""Build the one-page HTML report from a finished run.

Input is either the JSON written by `capitol_sdk.cli local --out file`, or a
run id in Postgres. Both hold the same records, so the report is the same.

Usage:
    python tools/make_report.py --json scratch/reference.json --out report.html
    python tools/make_report.py --run <run id>                --out report.html

With --research out/research.json (from tools/run_research.py) the report also
gets the deeper checks: benchmarks, sectors, committee match, copycat portfolio.
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Two series, checked for colour-blind separation; labels never rely on colour alone.
COLORS = {"trade": "#2a78d6", "disclosure": "#eb6834"}
LABELS = {"trade": "from the trade date", "disclosure": "from the disclosure date"}
MARKERS = {"trade": "o", "disclosure": "s"}
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
WINDOWS = (5, 20, 60)


def load(args) -> dict[tuple[str, str], dict]:
    if args.json:
        with open(args.json, encoding="utf-8") as f:
            rows = json.load(f)["records"]
        return {(r["kind"], r["key"]): r["data"] for r in rows}
    import psycopg

    dsn = os.environ.get("TEST_DSN", "postgresql://engine:engine@localhost:5433/engine")
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT kind, key, data FROM records WHERE run_id = %s", (args.run,)).fetchall()
    return {(kind, key): data for kind, key, data in rows}


def pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def chart(aggregates: dict[tuple[str, int, str], dict]) -> str:
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    xs = range(len(WINDOWS))
    for offset, start in ((-0.06, "trade"), (0.06, "disclosure")):
        rows = [aggregates[(start, w, "equal")] for w in WINDOWS]
        if any(r["mean"] is None for r in rows):
            continue
        means = [r["mean"] * 100 for r in rows]
        lows = [(r["mean"] - r["ci_low"]) * 100 for r in rows]
        highs = [(r["ci_high"] - r["mean"]) * 100 for r in rows]
        x = [i + offset for i in xs]
        ax.errorbar(x, means, yerr=[lows, highs], color=COLORS[start], marker=MARKERS[start], markersize=7,
                    linewidth=2, elinewidth=1.5, capsize=4, label=LABELS[start])
        ax.annotate(LABELS[start], (x[-1], means[-1]), xytext=(10, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.set_xticks(list(xs), [f"{w} trading days" for w in WINDOWS])
    ax.set_xlim(-0.4, len(WINDOWS) - 1 + 1.0)
    ax.set_ylabel("Mean return above the S&P 500, %", color=MUTED, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode()


def table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--json")
    source.add_argument("--run")
    parser.add_argument("--out", default="report.html")
    parser.add_argument("--research", help="JSON from tools/run_research.py; adds the deeper checks")
    args = parser.parse_args()

    research_html, extra_limits = "", ""
    if args.research:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import report_research

        with open(args.research, encoding="utf-8") as f:
            research_html = report_research.sections(json.load(f))
        extra_limits = report_research.EXTRA_LIMITS

    records = load(args)
    aggregates = {(d["start"], d["window"], d["weight"]): d for (kind, _), d in records.items() if kind == "aggregate"}
    trades = records.get(("trades_summary", "all"))
    study = records.get(("study_summary", "all"))
    if not aggregates or not trades or not study:
        print("the run has no aggregate, trades_summary or study_summary records; did it finish?")
        return 1

    headline = aggregates[("disclosure", 20, "equal")]
    at_trade = aggregates[("trade", 20, "equal")]
    crosses_zero = headline["ci_low"] is not None and headline["ci_low"] <= 0 <= headline["ci_high"]
    verdict = ("The interval includes zero: no clear edge for someone who copies the trades after disclosure."
               if crosses_zero else
               "The interval does not include zero. Read the limits below before trusting this.")

    result_rows = []
    for start in ("trade", "disclosure"):
        for window in WINDOWS:
            for weight in ("equal", "low", "mid", "high"):
                r = aggregates[(start, window, weight)]
                interval = "n/a" if r["mean"] is None else f"{pct(r['ci_low'])} to {pct(r['ci_high'])}"
                result_rows.append([LABELS[start], window, weight, pct(r["mean"]), interval, r["n"]])

    count_rows = [
        ["Filings in the index (PTRs)", trades["filings_found"]],
        ["Filings parsed", trades["filings_parsed"]],
        ["Filings skipped (scanned or not fetched)", trades["filings_skipped"]],
        ["Rows read from the filings", trades["rows"]],
        ["Common-stock trades kept", trades["trades"]],
        *[[f"Rejected: {reason}", n] for reason, n in trades["rejected"].items()],
        ["Trades with prices (studied)", study["studied"]],
        *[[f"Not studied: {reason}", n] for reason, n in study["skipped"].items()],
        ["Tickers with prices", study["tickers_with_prices"]],
        ["Tickers without prices", len(study["tickers_without_prices"])],
    ]

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>House stock trades: event study</title>
<style>
 body {{ font: 16px/1.5 system-ui, sans-serif; color: {INK}; background: {SURFACE}; max-width: 780px; margin: 2rem auto; padding: 0 16px; }}
 h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }} h2 {{ font-size: 1.15rem; margin-top: 2.4rem; }}
 h3 {{ font-size: 1rem; margin-top: 1.4rem; }}
 .sub, figcaption, .note {{ color: {MUTED}; font-size: .9rem; }}
 .headline {{ font-size: 1.1rem; border-left: 4px solid {COLORS['disclosure']}; padding: .4rem .9rem; margin: 1.2rem 0; }}
 img {{ max-width: 100%; height: auto; }} figure {{ margin: 1rem 0; }}
 .scroll {{ overflow-x: auto; }}
 table {{ border-collapse: collapse; font-size: .9rem; width: 100%; }}
 th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid {GRID}; white-space: nowrap; }}
 th {{ color: {MUTED}; font-weight: 600; }}
</style></head><body>
<h1>Do House members' stock trades beat the market?</h1>
<p class="sub">Periodic Transaction Reports filed with the Clerk of the House, 2024 and 2025. Descriptive only.</p>

<p class="headline">In the 20 trading days after a trade was <b>disclosed</b>, the traded stocks returned
<b>{pct(headline['mean'])}</b> against the S&amp;P 500 on average
(95% interval {pct(headline['ci_low'])} to {pct(headline['ci_high'])}, {headline['n']} trades).
Counted from the <b>trade date</b> itself: <b>{pct(at_trade['mean'])}</b>
({pct(at_trade['ci_low'])} to {pct(at_trade['ci_high'])}). {verdict}</p>

<figure>
 <img alt="Mean market-adjusted return after 5, 20 and 60 trading days, counted from the trade date and from the disclosure date, with 95% intervals. The numbers are in the results table below." src="data:image/png;base64,{chart(aggregates)}">
 <figcaption>Mean market-adjusted return with 95% bootstrap intervals, all trades weighted equally. Sales count with the sign flipped.</figcaption>
</figure>

<h2>Why two start dates</h2>
<p>The <b>trade date</b> line asks: did the member do well? The <b>disclosure date</b> line asks: could the public
have done well by copying the trade once it was published? Members have up to 45 days to report, so the two can differ.</p>

<h2>Results</h2>
<p class="note">Weight "equal" counts every trade once. Trades are reported in bands (for example $1,001 to $15,000), so
"low", "mid" and "high" weight each trade by that end of its band.</p>
<div class="scroll">{table(["Start", "Window (days)", "Weight", "Mean", "95% interval", "Trades"], result_rows)}</div>

{research_html}

<h2>Sample</h2>
<div class="scroll">{table(["Step", "Count"], count_rows)}</div>

<h2>Method</h2>
<p>Market-adjusted return = stock return minus the return of SPY (an S&amp;P 500 fund) over the same days, both with
dividends. A window starts on the first trading day after the event date. Intervals come from 2,000 seeded bootstrap resamples.</p>

<h2>Limits</h2>
<ul>
 <li><b>The intervals are too narrow.</b> The bootstrap treats trades as independent, but trades cluster in time and in the same stocks.</li>
 <li><b>Amounts are coarse.</b> Only a band is reported, so weighted results depend on which end of the band is used.</li>
 <li><b>Some filings are missing.</b> Scanned (paper) filings are skipped; there is no OCR. Amended rows and non-stock assets are left out.</li>
 <li><b>Some prices are missing.</b> Delisted tickers have no data in the price source, which can bias the result.</li>
{extra_limits} <li><b>No claim about any person.</b> This is an average over many filings. It cannot show intent or wrongdoing.</li>
</ul>
</body></html>
"""
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
