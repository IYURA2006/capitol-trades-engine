"""Link a filing to a House member and to that member's committees.

The link uses the seat ("SC03") and the date, never the name: one seat has one
member on any day. Names are only used afterwards, to check the link.
"""
from __future__ import annotations

# The 119th Congress began on this day; committee lists are kept per Congress.
CONGRESS_STARTS = (("119", "2025-01-03"), ("118", "2023-01-03"))


def parse_seat(state_district: str) -> tuple[str, int]:
    """"SC03" -> ("SC", 3). At-large seats and delegates are district 0."""
    return state_district[:2], int(state_district[2:] or 0)


def find_member(members: dict, state_district: str, on_date: str) -> str | None:
    """Id of the member who held the seat on that day, or None."""
    state, district = parse_seat(state_district)
    found = [member_id for member_id, info in members.items()
             if any(t["state"] == state and t["district"] == district and t["start"] <= on_date <= t["end"]
                    for t in info["terms"])]
    # Two ids on one day only happens on the day a seat changes hands.
    return min(found) if found else None


def congress_of(on_date: str) -> str | None:
    for congress, start in CONGRESS_STARTS:
        if on_date >= start:
            return congress
    return None


def committees_of(committees: dict, member_id: str, on_date: str) -> set[str]:
    """Committees of a member in the Congress that sat on that day.

    The lists are one snapshot per Congress, so a member who changed
    committee in the middle of a Congress is placed by the snapshot.
    """
    congress = congress_of(on_date)
    if congress is None or congress not in committees["congresses"]:
        return set()
    return {committee for committee, ids in committees["congresses"][congress]["committees"].items()
            if member_id in ids}
