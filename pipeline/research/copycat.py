"""Copycat portfolio: buy what members bought, on the day after it became public.

This is a calendar-time portfolio, the usual tool in the papers on this
topic. Every purchase is bought at the close of the first trading day after
its disclosure and held for `hold` trading days. On each day the portfolio
return is the plain average over the positions it holds. Because the result
is one return per day, trades that cluster in time do not count many times.
"""
from __future__ import annotations

from bisect import bisect_right

import numpy as np


def daily_returns(positions: list[tuple[str, str]], series: dict[str, tuple[list[str], list[float]]],
                  hold: int) -> dict[str, tuple[float, int]]:
    """positions: (ticker, disclosure date). Returns day -> (portfolio return, positions held)."""
    total: dict[str, float] = {}
    count: dict[str, int] = {}
    for ticker, disclosed in positions:
        dates, closes = series[ticker]
        entry = bisect_right(dates, disclosed)  # bought at this day's close
        for i in range(entry + 1, min(entry + hold, len(dates) - 1) + 1):
            if closes[i - 1] <= 0:
                continue
            day = dates[i]
            total[day] = total.get(day, 0.0) + closes[i] / closes[i - 1] - 1
            count[day] = count.get(day, 0) + 1
    return {day: (total[day] / count[day], count[day]) for day in sorted(total)}


def fund_daily(fund: dict[str, float], days: list[str]) -> dict[str, float]:
    """Daily returns of a fund on the given days (needs the day before, too)."""
    ordered = sorted(fund)
    before = {day: ordered[i - 1] for i, day in enumerate(ordered) if i > 0}
    return {day: fund[day] / fund[before[day]] - 1 for day in days if day in before}


def block_interval(values: np.ndarray, rng: np.random.Generator, block: int = 20, resamples: int = 2000,
                   level: float = 0.95) -> tuple[float, float]:
    """Interval of the mean of a daily series. Whole blocks of days are drawn,
    because returns on nearby days are not independent."""
    n = len(values)
    blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(resamples, blocks))
    index = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(resamples, -1)[:, :n]
    means = values[index].mean(axis=1)
    tail = (1 - level) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(low), float(high)


def compare(portfolio: dict[str, tuple[float, int]], fund: dict[str, float], rng: np.random.Generator,
            min_positions: int = 10) -> dict:
    """Portfolio against one fund on the days when the portfolio holds enough positions."""
    days = [day for day, (_, held) in portfolio.items() if held >= min_positions]
    bench = fund_daily(fund, days)
    days = [day for day in days if day in bench]
    if len(days) < 40:
        return {"days": len(days)}
    ours = np.array([portfolio[day][0] for day in days])
    theirs = np.array([bench[day] for day in days])
    excess = ours - theirs
    low, high = block_interval(excess, rng)
    # Market model: ours = alpha + beta * theirs. Alpha is the part of the
    # return that the market's own moves do not explain.
    beta, alpha = np.polyfit(theirs, ours, 1)
    return {
        "days": len(days), "first": days[0], "last": days[-1],
        "avg_positions": float(np.mean([portfolio[day][1] for day in days])),
        "total": float(np.prod(1 + ours) - 1), "bench_total": float(np.prod(1 + theirs) - 1),
        "excess_per_year": float(excess.mean() * 252),
        "ci_low": low * 252, "ci_high": high * 252,
        "alpha_per_year": float(alpha * 252), "beta": float(beta),
        "curve": [[day, round(float(a), 5), round(float(b), 5)] for day, a, b in
                  zip(days, np.cumprod(1 + ours) - 1, np.cumprod(1 + theirs) - 1, strict=True)],
    }
