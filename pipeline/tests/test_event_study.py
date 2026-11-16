"""Event study on synthetic prices, where the right answer is known."""
from datetime import date, timedelta

import numpy as np
import pytest

from pipeline.finance import event_study as es


def weekdays(start: date, n: int) -> list[str]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


DAYS = weekdays(date(2024, 1, 1), 120)  # 2024-01-01 is a Monday


def growing(daily: float, first: float = 100.0) -> list[list]:
    return [[d, first * (1 + daily) ** i] for i, d in enumerate(DAYS)]


STOCK = es.split(growing(0.01))  # +1% every trading day
BENCH = dict(zip(*es.split(growing(0.005)), strict=True))  # +0.5% every trading day


@pytest.mark.parametrize("window", [5, 20, 60])
def test_abnormal_return_is_stock_minus_benchmark(window):
    expected = (1.01 ** window - 1) - (1.005 ** window - 1)
    assert es.abnormal_return(STOCK, BENCH, DAYS[3], window) == pytest.approx(expected, abs=1e-12)


def test_window_starts_on_the_first_trading_day_after_the_event():
    # Event on a Saturday: the window starts on Monday, like an event on Friday.
    friday = DAYS[4]
    saturday = (date.fromisoformat(friday) + timedelta(days=1)).isoformat()
    assert es.abnormal_return(STOCK, BENCH, saturday, 5) == es.abnormal_return(STOCK, BENCH, friday, 5)

    # Prices jump once, between day 10 and day 11. An event ON day 10 starts
    # at day 11, after the jump, so it must not get the jump's return.
    jump = [[d, 100.0 if i <= 10 else 150.0] for i, d in enumerate(DAYS)]
    flat = {d: 100.0 for d in DAYS}
    assert es.abnormal_return(es.split(jump), flat, DAYS[10], 5) == 0.0
    assert es.abnormal_return(es.split(jump), flat, DAYS[9], 5) == pytest.approx(0.5)


def test_missing_prices_give_none():
    assert es.abnormal_return(STOCK, BENCH, DAYS[-3], 5) is None  # window runs past the data
    assert es.abnormal_return(STOCK, BENCH, "2030-01-01", 5) is None  # event after the data
    holes = {d: p for d, p in BENCH.items() if d != DAYS[9]}
    assert es.abnormal_return(STOCK, holes, DAYS[3], 5) is None  # benchmark has no price on the end day
    assert es.abnormal_return(([], []), BENCH, DAYS[3], 5) is None


def test_sales_are_counted_with_the_sign_flipped():
    trade = {"side": "buy", "trade_date": DAYS[3], "disclosure_date": DAYS[30]}
    buy = es.study_trade(trade, STOCK, BENCH)
    sell = es.study_trade({**trade, "side": "sell"}, STOCK, BENCH)
    for start in es.STARTS:
        for window in ("5", "20", "60"):
            assert sell[start][window] == pytest.approx(-buy[start][window])
    assert buy["trade"]["5"] > 0  # the stock beats the benchmark by construction


def test_trade_date_and_disclosure_date_give_different_windows():
    # The stock only beats the market in its first 20 days.
    early = [[d, 100.0 * (1.02 ** min(i, 20))] for i, d in enumerate(DAYS)]
    flat = {d: 100.0 for d in DAYS}
    trade = {"side": "buy", "trade_date": DAYS[0], "disclosure_date": DAYS[40]}
    study = es.study_trade(trade, es.split(early), flat)
    assert study["trade"]["5"] > 0.09
    assert study["disclosure"]["5"] == 0.0  # by the disclosure date the edge is gone


def test_amount_weights():
    trade = {"amount_low": 1001.0, "amount_high": 15000.0}
    assert [es.amount_weight(trade, k) for k in es.WEIGHTS] == [1.0, 1001.0, 8000.5, 15000.0]
    values, weights = np.array([0.10, -0.10]), np.array([3.0, 1.0])
    assert es.weighted_mean(values, weights) == pytest.approx(0.05)


def test_bootstrap_interval_contains_the_mean_and_is_seeded():
    values = np.random.default_rng(1).normal(0.02, 0.05, size=400)
    weights = np.ones(400)
    low, high = es.bootstrap_interval(values, weights, np.random.default_rng(7), resamples=500)
    assert low < values.mean() < high
    # About 2 standard errors on each side: 2 * 0.05 / sqrt(400) = 0.005.
    assert 0.007 < high - low < 0.013
    again = es.bootstrap_interval(values, weights, np.random.default_rng(7), resamples=500)
    assert (low, high) == again


def test_bootstrap_of_identical_values_has_zero_width():
    values, weights = np.full(50, 0.03), np.ones(50)
    low, high = es.bootstrap_interval(values, weights, np.random.default_rng(0), resamples=200)
    assert low == pytest.approx(0.03) and high == pytest.approx(0.03)


def test_aggregate_counts_only_trades_with_prices():
    trade = {"side": "buy", "trade_date": DAYS[3], "disclosure_date": DAYS[100],
             "amount_low": 1001.0, "amount_high": 15000.0}
    results = [(trade, es.study_trade(trade, STOCK, BENCH)) for _ in range(5)]
    rows = es.aggregate(results, np.random.default_rng(0), resamples=50)
    assert len(rows) == 2 * 3 * 4
    by = {(r["start"], r["window"], r["weight"]): r for r in rows}
    assert by[("trade", 60, "equal")]["n"] == 5
    assert by[("trade", 5, "mid")]["mean"] == pytest.approx((1.01 ** 5) - (1.005 ** 5))
    # Disclosure on day 100 of 120: the 5-day window fits, the 20- and 60-day windows do not.
    assert by[("disclosure", 5, "equal")]["n"] == 5
    assert by[("disclosure", 20, "equal")]["n"] == 0
    assert by[("disclosure", 60, "equal")]["mean"] is None
