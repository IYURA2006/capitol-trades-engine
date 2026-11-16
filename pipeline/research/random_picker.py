"""Random picker test: skill or luck?

Idea from Belmont, Sacerdote, Sehgal and Van Hoek (2022). Replace every
purchase with a random stock from the same sector, bought on the same day and
held for the same time. Do that many times. If the real purchases are not
better than most of these random portfolios, the data does not show skill.
"""
from __future__ import annotations

import numpy as np


def simulate(candidates: list[tuple[np.ndarray, np.ndarray | None]], rng: np.random.Generator,
             draws: int = 1000) -> np.ndarray:
    """candidates: for every purchase, the returns of all stocks it could be
    replaced with, and optional draw weights. Returns the mean return of each
    random portfolio."""
    total = np.zeros(draws)
    for returns, weights in candidates:
        p = None if weights is None else weights / weights.sum()
        total += returns[rng.choice(len(returns), size=draws, p=p)]
    return total / len(candidates)


def percentile(real: float, random_means: np.ndarray) -> float:
    """Share of random portfolios that the real one beats (0 to 1)."""
    return float(np.mean(random_means < real))


def run(real_returns: list[float], candidates: list[tuple[np.ndarray, np.ndarray | None]],
         rng: np.random.Generator, draws: int = 1000) -> dict:
    real = float(np.mean(real_returns))
    random_means = simulate(candidates, rng, draws)
    low, high = np.quantile(random_means, [0.025, 0.975])
    return {"n": len(real_returns), "real": real, "random_mean": float(random_means.mean()),
            "random_low": float(low), "random_high": float(high), "percentile": percentile(real, random_means),
            "random_means": random_means}
