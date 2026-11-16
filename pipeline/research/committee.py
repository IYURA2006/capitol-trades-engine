"""Committee match: is a stock in an industry that the member's committee oversees?

The mapping is pipeline/data/committee_industries.json. It was fixed before
any result was computed.
"""
from __future__ import annotations


def overseen(rule: dict, sector: str, industry: str) -> bool:
    return sector in rule["sectors"] or industry in rule["industries"]


def overseers(mapping: dict, sector: str, industry: str) -> set[str]:
    """Committees in this mapping group that oversee the stock's industry."""
    return {committee for committee, rule in mapping.items() if overseen(rule, sector, industry)}


def classify(mapping: dict, member_committees: set[str], sector: str, industry: str) -> str:
    """"matched": the member sits on a committee that oversees the industry.
    "same_industry": some committee oversees it, but not one of the member's.
    "other": no committee in the mapping oversees it.

    The fair comparison is matched against same_industry: the same kind of
    stock, bought by members with and without a seat on the committee.
    """
    watching = overseers(mapping, sector, industry)
    if not watching:
        return "other"
    return "matched" if watching & member_committees else "same_industry"
