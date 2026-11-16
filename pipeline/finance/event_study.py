"""Event study: did a traded stock beat the market after the trade?

Pure functions. Prices are lists of [iso_date, adjusted_close], oldest first.

The measure is the market-adjusted return:

    abnormal return = stock return - benchmark return   (same days)

It needs no model to fit, so it is easy to check by hand. A sale counts with
the sign flipped: a stock that falls after it was sold is a "good" trade.
"""
from __future__ import annotations

from bisect import bisect_right

import numpy as np

WINDOWS = (5, 20, 60)  # trading days
STARTS = ("trade", "disclosure")
WEIGHTS = ("equal", "low", "mid", "high")


def split(prices: list[list]) -> tuple[list[str], list[float]]:
    """[[date, close], ...] -> (dates, closes), sorted by date."""
    rows = sorted((str(d), float(c)) for d, c in prices)
    return [d for d, _ in rows], [c for _, c in rows]


def abnormal_return(stock: tuple[list[str], list[float]], bench: dict[str, float],
                    event_date: str, window: int) -> float | None:
    """Return over `window` trading days, starting on the first trading day
    AFTER event_date, minus the benchmark's return over the same two days.

    None when a price is missing (too early, too late, or a gap).
    """
    dates, closes = stock
    start = bisect_right(dates, event_date)  # first index with date > event_date
    end = start + window
    if end >= len(dates):
        return None
    d0, d1 = dates[start], dates[end]
    if d0 not in bench or d1 not in bench:
        return None
    if closes[start] <= 0 or bench[d0] <= 0:
        return None
    stock_return = closes[end] / closes[start] - 1
    bench_return = bench[d1] / bench[d0] - 1
    return stock_return - bench_return


def study_trade(trade: dict, stock: tuple[list[str], list[float]], bench: dict[str, float]) -> dict:
    """All (start, window) abnormal returns for one trade. Sales are flipped."""
    sign = 1.0 if trade["side"] == "buy" else -1.0
    out: dict[str, dict[str, float | None]] = {}
    for start in STARTS:
        event_date = trade["trade_date"] if start == "trade" else trade["disclosure_date"]
        out[start] = {}
        for window in WINDOWS:
            ar = abnormal_return(stock, bench, event_date, window)
            out[start][str(window)] = None if ar is None else sign * ar
    return out


def amount_weight(trade: dict, kind: str) -> float:
    """Trades are reported in bands. Use the low end, the middle or the high
    end of the band as the weight, to see if the answer depends on it."""
    low, high = trade["amount_low"], trade["amount_high"]
    return {"equal": 1.0, "low": low, "mid": (low + high) / 2, "high": high}[kind]


def weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    return float(np.sum(values * weights) / np.sum(weights))


def bootstrap_interval(values: np.ndarray, weights: np.ndarray, rng: np.random.Generator,
                       resamples: int = 2000, level: float = 0.95) -> tuple[float, float]:
    """Percentile bootstrap: draw the trades again with replacement, many
    times, and see how much the mean moves.

    Limit: it treats trades as independent. Real trades cluster in time and
    in the same stocks, so the true interval is wider than this one.
    """
    n = len(values)
    means = np.empty(resamples)
    chunk = 100  # keep memory small: chunk * n indexes at a time
    for first in range(0, resamples, chunk):
        size = min(chunk, resamples - first)
        index = rng.integers(0, n, size=(size, n))
        v, w = values[index], weights[index]
        means[first:first + size] = (v * w).sum(axis=1) / w.sum(axis=1)
    tail = (1 - level) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(low), float(high)


def aggregate(results: list[tuple[dict, dict]], rng: np.random.Generator, resamples: int = 2000) -> list[dict]:
    """results: (trade, study) pairs in a fixed order. One output row per
    (start, window, weight) with mean, 95% interval and sample size."""
    rows = []
    for start in STARTS:
        for window in WINDOWS:
            usable = [(t, s[start][str(window)]) for t, s in results if s[start][str(window)] is not None]
            for weight in WEIGHTS:
                row = {"start": start, "window": window, "weight": weight, "n": len(usable),
                       "mean": None, "ci_low": None, "ci_high": None}
                if len(usable) >= 2:
                    values = np.array([ar for _, ar in usable], dtype=float)
                    weights = np.array([amount_weight(t, weight) for t, _ in usable], dtype=float)
                    row["mean"] = weighted_mean(values, weights)
                    row["ci_low"], row["ci_high"] = bootstrap_interval(values, weights, rng, resamples)
                rows.append(row)
    return rows


def window_return(stock: tuple[list[str], list[float]], event_date: str, window: int,
                  from_event_close: bool = False) -> tuple[str, str, float] | None:
    """(first day, last day, return) of one stock over `window` trading days.

    By default the window starts on the first trading day AFTER event_date,
    like abnormal_return. With from_event_close it starts at the last close on
    or before event_date, which is what a study of the reaction to news needs.
    None when the prices do not cover the window.
    """
    dates, closes = stock
    start = bisect_right(dates, event_date) - (1 if from_event_close else 0)
    end = start + window
    if start < 0 or end >= len(dates) or closes[start] <= 0:
        return None
    return dates[start], dates[end], closes[end] / closes[start] - 1
