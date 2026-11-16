"""Research checks on synthetic data, where the right answer is known."""
from datetime import date, timedelta

import numpy as np
import pytest

from pipeline.finance import event_study as es
from pipeline.research import committee, copycat, members, random_picker, returns, splits


def weekdays(start: date, n: int) -> list[str]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


DAYS = weekdays(date(2024, 1, 1), 300)


def growing(daily: float, first: float = 100.0) -> tuple[list[str], list[float]]:
    return DAYS, [first * (1 + daily) ** i for i in range(len(DAYS))]


STOCK = growing(0.01)
FUNDS = {"SPY": dict(zip(*growing(0.005), strict=True)), "RSP": dict(zip(*growing(0.002), strict=True)),
         "XLK": dict(zip(*growing(0.008), strict=True))}


# ---- one trade ---------------------------------------------------------------

def test_window_return_starts_after_the_event_by_default():
    first, last, value = es.window_return(STOCK, DAYS[3], 20)
    assert (first, last) == (DAYS[4], DAYS[24])
    assert value == pytest.approx(1.01 ** 20 - 1)


def test_window_return_can_start_at_the_close_of_the_event_day():
    first, last, value = es.window_return(STOCK, DAYS[3], 1, from_event_close=True)
    assert (first, last) == (DAYS[3], DAYS[4])
    assert value == pytest.approx(0.01)


def test_window_return_is_none_outside_the_prices():
    assert es.window_return(STOCK, DAYS[-5], 20) is None
    assert es.window_return(STOCK, "2023-12-01", 1, from_event_close=True) is None


def test_measure_gives_every_benchmark_over_the_same_days():
    row = returns.measure(STOCK, FUNDS, "Technology", DAYS[0], 10)
    assert row["stock"] == pytest.approx(1.01 ** 10 - 1)
    assert row["SPY"] == pytest.approx(1.005 ** 10 - 1)
    assert row["RSP"] == pytest.approx(1.002 ** 10 - 1)
    assert row["sector"] == pytest.approx(1.008 ** 10 - 1)  # Technology -> XLK
    assert row["QQQ"] is None  # no such fund in this test


def test_measure_has_no_sector_benchmark_for_an_unknown_sector():
    assert returns.measure(STOCK, FUNDS, "", DAYS[0], 10)["sector"] is None


# ---- many trades -------------------------------------------------------------

def rows_for(members_and_excess: list[tuple[str, float]]) -> list[dict]:
    return [{"member": m, "stock": 0.10 + x, "SPY": 0.10} for m, x in members_and_excess]


def test_summarize_counts_and_averages():
    rows = rows_for([("a", 0.02), ("a", -0.01), ("b", 0.05), ("c", 0.00)])
    out = returns.summarize(rows, "SPY", np.random.default_rng(1))
    assert (out["n"], out["members"]) == (4, 3)
    assert out["excess"] == pytest.approx(0.015)
    assert out["median"] == pytest.approx(0.01)
    assert out["beat"] == pytest.approx(0.5)
    assert out["ci_low"] <= out["excess"] <= out["ci_high"]


def test_summarize_skips_rows_without_that_benchmark():
    rows = [*rows_for([("a", 0.02), ("b", 0.04)]), {"member": "c", "stock": 9.0, "SPY": None}]
    assert returns.summarize(rows, "SPY", np.random.default_rng(1))["n"] == 2


def test_interval_by_member_is_wider_when_one_members_trades_are_alike():
    # 10 members, 50 trades each, and all trades of one member are the same.
    rng = np.random.default_rng(3)
    member_effect = rng.normal(0, 0.05, size=10)
    values = np.repeat(member_effect, 50)
    groups = [f"m{i}" for i in range(10) for _ in range(50)]
    low, high = returns.cluster_interval(values, groups, np.random.default_rng(1))
    as_if_independent = returns.cluster_interval(values, list(range(500)), np.random.default_rng(1))
    assert (high - low) > 3 * (as_if_independent[1] - as_if_independent[0])


def test_interval_is_the_same_for_the_same_seed_and_missing_for_two_members():
    values, groups = [0.1, 0.2, 0.3, 0.4], ["a", "b", "c", "c"]
    assert (returns.cluster_interval(values, groups, np.random.default_rng(5))
            == returns.cluster_interval(values, groups, np.random.default_rng(5)))
    assert returns.cluster_interval([0.1, 0.2], ["a", "b"], np.random.default_rng(5)) is None


def test_difference_between_two_groups():
    rows = rows_for([("a", 0.06), ("b", 0.04), ("c", 0.05), ("a", 0.01), ("b", 0.00), ("d", 0.02)])
    for i, row in enumerate(rows):
        row["flag"] = i < 3
    out = returns.difference(rows, "flag", "SPY", np.random.default_rng(1))
    assert out["yes"]["n"] == 3 and out["no"]["n"] == 3
    assert out["diff"] == pytest.approx(0.05 - 0.01)
    assert out["ci_low"] <= out["diff"] <= out["ci_high"]


# ---- members and committees --------------------------------------------------

MEMBERS = {
    "A000001": {"last": "Old", "terms": [{"state": "SC", "district": 3, "start": "2023-01-03", "end": "2025-01-03"}]},
    "B000002": {"last": "New", "terms": [{"state": "SC", "district": 3, "start": "2025-01-03", "end": "2027-01-03"}]},
    "C000003": {"last": "Large", "terms": [{"state": "AK", "district": 0, "start": "2023-01-03", "end": "2027-01-03"}]},
}
COMMITTEES = {"congresses": {"118": {"committees": {"HSAS": ["A000001"], "HSBA": ["C000003"]}},
                             "119": {"committees": {"HSAS": ["B000002", "C000003"]}}}}


def test_seat_and_date_find_the_member():
    assert members.parse_seat("SC03") == ("SC", 3)
    assert members.parse_seat("AK00") == ("AK", 0)
    assert members.find_member(MEMBERS, "SC03", "2024-06-01") == "A000001"
    assert members.find_member(MEMBERS, "SC03", "2025-06-01") == "B000002"
    assert members.find_member(MEMBERS, "AK00", "2024-06-01") == "C000003"
    assert members.find_member(MEMBERS, "TX01", "2024-06-01") is None


def test_committees_come_from_the_congress_of_that_day():
    assert members.committees_of(COMMITTEES, "C000003", "2024-12-31") == {"HSBA"}
    assert members.committees_of(COMMITTEES, "C000003", "2025-01-03") == {"HSAS"}
    assert members.committees_of(COMMITTEES, "C000003", "2022-06-01") == set()


MAPPING = {"HSAS": {"sectors": [], "industries": ["Aerospace & Defense"]},
           "HSBA": {"sectors": ["Financial Services"], "industries": []}}


def test_trade_is_matched_only_when_the_members_own_committee_oversees_it():
    assert committee.classify(MAPPING, {"HSAS"}, "Industrials", "Aerospace & Defense") == "matched"
    assert committee.classify(MAPPING, {"HSBA"}, "Industrials", "Aerospace & Defense") == "same_industry"
    assert committee.classify(MAPPING, {"HSBA"}, "Financial Services", "Banks - Regional") == "matched"
    assert committee.classify(MAPPING, {"HSAS"}, "Industrials", "Railroads") == "other"
    assert committee.classify(MAPPING, set(), "Technology", "Semiconductors") == "other"


# ---- copycat portfolio -------------------------------------------------------

def test_copycat_holds_each_purchase_for_the_hold_time_only():
    out = copycat.daily_returns([("X", DAYS[9])], {"X": STOCK}, hold=5)
    # Bought at the close of DAYS[10]; earns the next 5 daily moves.
    assert list(out) == DAYS[11:16]
    assert all(r == pytest.approx(0.01) and held == 1 for r, held in out.values())


def test_copycat_averages_the_positions_it_holds():
    flat = (DAYS, [50.0] * len(DAYS))
    out = copycat.daily_returns([("X", DAYS[9]), ("FLAT", DAYS[11])], {"X": STOCK, "FLAT": flat}, hold=5)
    assert out[DAYS[11]] == (pytest.approx(0.01), 1)
    assert out[DAYS[13]] == (pytest.approx(0.005), 2)
    assert out[DAYS[17]] == (pytest.approx(0.0), 1)


def test_copycat_compare_finds_alpha_and_beta():
    rng = np.random.default_rng(2)
    market = rng.normal(0.0004, 0.01, size=len(DAYS) - 1)
    fund_closes = np.concatenate([[100.0], 100.0 * np.cumprod(1 + market)])
    fund = dict(zip(DAYS, fund_closes, strict=True))
    # The portfolio moves twice as much as the market and adds 0.1% a day.
    portfolio = {day: (2 * m + 0.001, 25) for day, m in zip(DAYS[1:], market, strict=True)}
    out = copycat.compare(portfolio, fund, np.random.default_rng(1))
    assert out["days"] == len(DAYS) - 1
    assert out["beta"] == pytest.approx(2.0)
    assert out["alpha_per_year"] == pytest.approx(0.001 * 252)
    assert out["ci_low"] <= out["excess_per_year"] <= out["ci_high"]
    assert out["curve"][-1][0] == DAYS[-1]


def test_copycat_ignores_days_with_too_few_positions():
    portfolio = {day: (0.01, 3) for day in DAYS[1:]}
    assert copycat.compare(portfolio, FUNDS["SPY"], np.random.default_rng(1), min_positions=10) == {"days": 0}


# ---- random picker -----------------------------------------------------------

def test_random_picker_ranks_the_real_result_among_random_ones():
    rng = np.random.default_rng(4)
    candidates = [(rng.normal(0.05, 0.2, size=40), None) for _ in range(200)]
    out = random_picker.run([0.05] * 200, candidates, np.random.default_rng(1), draws=500)
    assert 0.2 < out["percentile"] < 0.8          # as good as a random pick
    best = [float(c.max()) for c, _ in candidates]
    assert random_picker.run(best, candidates, np.random.default_rng(1), draws=500)["percentile"] == 1.0
    worst = [float(c.min()) for c, _ in candidates]
    assert random_picker.run(worst, candidates, np.random.default_rng(1), draws=500)["percentile"] == 0.0


def test_random_picker_follows_the_draw_weights():
    candidates = [(np.array([0.0, 1.0]), np.array([0.0, 5.0]))] * 10
    assert random_picker.simulate(candidates, np.random.default_rng(1), draws=50) == pytest.approx(np.ones(50))


# ---- splits ------------------------------------------------------------------

def test_late_means_more_than_45_days_after_the_trade():
    trade = {"trade_date": "2024-01-01", "disclosure_date": "2024-02-15", "state_district": "TX07"}
    assert splits.days_to_disclosure(trade) == 45 and not splits.is_late(trade)
    assert splits.is_late({**trade, "disclosure_date": "2024-02-16"})


def test_home_state_needs_a_known_head_office():
    trade = {"state_district": "TX07"}
    assert splits.is_home_state(trade, "TX") is True
    assert splits.is_home_state(trade, "CA") is False
    assert splits.is_home_state(trade, "") is None
