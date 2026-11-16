"""Small yes/no questions about one trade, used to split the sample in two."""
from __future__ import annotations

from datetime import date

LATE_AFTER_DAYS = 45  # the STOCK Act limit, counted from the trade date

# Stocks treated as "AI names". The list was made in November 2026, knowing
# which stocks had risen. It describes the sample; it is not a test of skill.
MAG7 = frozenset({"NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "AAPL", "TSLA"})
AI_NAMES = MAG7 | frozenset({
    "AVGO", "AMD", "PLTR", "TSM", "ORCL", "SMCI", "MU", "ASML", "ANET", "DELL", "VRT", "MRVL", "ARM", "SNOW",
    "NOW", "CRM", "AMAT", "LRCX", "KLAC", "QCOM", "INTC", "CRWV", "VST", "CEG"})


def days_to_disclosure(trade: dict) -> int:
    return (date.fromisoformat(trade["disclosure_date"]) - date.fromisoformat(trade["trade_date"])).days


def is_late(trade: dict) -> bool:
    """Disclosed more than 45 days after the trade."""
    return days_to_disclosure(trade) > LATE_AFTER_DAYS


def is_home_state(trade: dict, company_state: str) -> bool | None:
    """Is the company's head office in the member's state? None when the
    head office is unknown or outside the United States."""
    if not company_state:
        return None
    return company_state == trade["state_district"][:2]


def is_ai_name(ticker: str) -> bool:
    return ticker in AI_NAMES
