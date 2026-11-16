"""Returns of single trades against several benchmarks, and honest intervals.

Pure functions. A "row" is one trade with its measured returns:
    {"member": ..., "stock": 0.12, "SPY": 0.10, "RSP": 0.07, "QQQ": 0.13, "sector": 0.09, ...}
A benchmark value is None when that fund has no price on the two days.
"""
from __future__ import annotations

import numpy as np

from ..finance import event_study as es

BENCHMARKS = ("SPY", "RSP", "QQQ", "sector")
BENCHMARK_LABELS = {"SPY": "S&P 500", "RSP": "equal-weight S&P 500", "QQQ": "Nasdaq-100",
                    "sector": "fund of the stock's own sector"}
# Yahoo sector name -> the sector fund used as its benchmark.
SECTOR_FUND = {"Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV", "Energy": "XLE",
               "Industrials": "XLI", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Utilities": "XLU",
               "Basic Materials": "XLB", "Real Estate": "XLRE", "Communication Services": "XLC"}


def fund_return(fund: dict[str, float] | None, first_day: str, last_day: str) -> float | None:
    if not fund or first_day not in fund or last_day not in fund or fund[first_day] <= 0:
        return None
    return fund[last_day] / fund[first_day] - 1


def measure(stock: tuple[list[str], list[float]], funds: dict[str, dict[str, float]], sector: str,
            event_date: str, window: int, from_event_close: bool = False) -> dict | None:
    """Return of one stock over the window, and of each benchmark over the same two days."""
    found = es.window_return(stock, event_date, window, from_event_close)
    if found is None:
        return None
    first_day, last_day, stock_return = found
    row: dict = {"stock": stock_return}
    for name in ("SPY", "RSP", "QQQ"):
        row[name] = fund_return(funds.get(name), first_day, last_day)
    row["sector"] = fund_return(funds.get(SECTOR_FUND.get(sector, "")), first_day, last_day)
    return row


def _cluster_sums(values: np.ndarray, groups: list) -> tuple[np.ndarray, np.ndarray]:
    index: dict = {}
    for g in groups:
        index.setdefault(g, len(index))
    which = np.array([index[g] for g in groups])
    sums = np.bincount(which, weights=values, minlength=len(index))
    counts = np.bincount(which, minlength=len(index)).astype(float)
    return sums, counts


def cluster_interval(values, groups: list, rng: np.random.Generator, resamples: int = 2000,
                     level: float = 0.95) -> tuple[float, float] | None:
    """Bootstrap interval of the mean that draws whole members, not single trades.

    A few members make most of the trades, and one member's trades are alike.
    Drawing members keeps that dependence, so the interval is wider and more
    honest than one that treats every trade as independent.
    """
    sums, counts = _cluster_sums(np.asarray(values, dtype=float), groups)
    if len(sums) < 3:
        return None
    draw = rng.integers(0, len(sums), size=(resamples, len(sums)))
    means = sums[draw].sum(axis=1) / counts[draw].sum(axis=1)
    tail = (1 - level) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(low), float(high)


def summarize(rows: list[dict], against: str, rng: np.random.Generator, resamples: int = 2000) -> dict:
    """Mean stock return, mean benchmark return and the difference, for rows
    that have this benchmark."""
    use = [r for r in rows if r.get(against) is not None]
    out = {"n": len(use), "members": len({r["member"] for r in use}), "stock": None, "bench": None,
           "excess": None, "median": None, "beat": None, "ci_low": None, "ci_high": None}
    if len(use) < 2:
        return out
    stock = np.array([r["stock"] for r in use])
    bench = np.array([r[against] for r in use])
    excess = stock - bench
    out.update(stock=float(stock.mean()), bench=float(bench.mean()), excess=float(excess.mean()),
               median=float(np.median(excess)), beat=float(np.mean(excess > 0)))
    interval = cluster_interval(excess, [r["member"] for r in use], rng, resamples)
    if interval:
        out["ci_low"], out["ci_high"] = interval
    return out


def difference(rows: list[dict], flag: str, against: str, rng: np.random.Generator,
               resamples: int = 2000, level: float = 0.95) -> dict:
    """Mean excess return of rows where row[flag] is true, minus the rest.

    The interval draws members again, and both groups come from the same draw.
    """
    use = [r for r in rows if r.get(against) is not None]
    yes = [r for r in use if r[flag]]
    no = [r for r in use if not r[flag]]
    out = {"yes": summarize(yes, against, rng, resamples), "no": summarize(no, against, rng, resamples),
           "diff": None, "ci_low": None, "ci_high": None}
    if len(yes) < 2 or len(no) < 2:
        return out
    excess = np.array([r["stock"] - r[against] for r in use])
    flags = np.array([bool(r[flag]) for r in use])
    groups = [r["member"] for r in use]
    # Per member: sum and count of the excess returns in each of the two groups.
    sum_yes = _cluster_sums(np.where(flags, excess, 0.0), groups)[0]
    n_yes = _cluster_sums(flags.astype(float), groups)[0]
    sum_no = _cluster_sums(np.where(flags, 0.0, excess), groups)[0]
    n_no = _cluster_sums((~flags).astype(float), groups)[0]
    out["diff"] = float(excess[flags].mean() - excess[~flags].mean())
    if len(sum_yes) < 3:
        return out
    draw = rng.integers(0, len(sum_yes), size=(resamples, len(sum_yes)))
    with np.errstate(invalid="ignore", divide="ignore"):
        diffs = sum_yes[draw].sum(axis=1) / n_yes[draw].sum(axis=1) - sum_no[draw].sum(axis=1) / n_no[draw].sum(axis=1)
    diffs = diffs[np.isfinite(diffs)]
    if len(diffs) >= resamples // 2:
        tail = (1 - level) / 2
        low, high = np.quantile(diffs, [tail, 1 - tail])
        out["ci_low"], out["ci_high"] = float(low), float(high)
    return out
