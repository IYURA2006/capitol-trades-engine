"""PTR text parsing on small hand-written examples of every layout seen."""
from pipeline import ptr

FILING = {"doc_id": "1", "first": "Ann", "last": "Example", "state_district": "ZZ01", "filing_date": "2025-04-04"}

TEXT = """Filing ID #1
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
SP Altria Group, Inc. (MO) [ST] S 03/27/2025 04/04/2025 $1,001 - $15,000
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New
S\x00\x00\x00\x00\x00\x00\x00\x00\x00 O\x00: Trust One
SP Alphabet Inc. - Class A Common S (partial) 03/27/2025 04/04/2025 $15,001 -
Stock (GOOGL) [ST] $50,000
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New
BlackRock, Inc. Common Stock (BLK) P 01/02/2025 01/03/2025 $1,001 - $15,000
[ST]
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New
JT U.S Treasury Bills [GS] P 12/27/2024 12/31/2024 Spouse/DC Over
$1,000,000
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New
Broadcom Inc. - Common Stock P 01/29/2025 02/06/2025 $1,001 - $15,000
(AVGO) [OP]
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New
D\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00: Put option, strike price $215, exp. Feb. 21, 2025.
DC Berkshire Hathaway Inc. New (BRK.B) [ST] E 02/03/2025 02/06/2025 $1,140.00
F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: Amended
* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.
I CERTIFY that the statements I have made S 01/01/2025 01/02/2025 $1 are true
"""


def rows():
    return ptr.parse_ptr_text(TEXT)


def test_every_row_is_found_and_nothing_after_the_table():
    assert [(r["owner"], r["type"]) for r in rows()] == [
        ("SP", "S"), ("SP", "S (partial)"), ("", "P"), ("JT", "P"), ("", "P"), ("DC", "E")]


def test_wrapped_asset_name_and_wrapped_amount():
    r = rows()[1]
    assert r["asset"] == "Alphabet Inc. - Class A Common Stock (GOOGL) [ST]"
    assert r["amount"] == "$15,001 - $50,000"
    assert ptr.parse_amount(r["amount"]) == (15001.0, 50000.0)


def test_page_break_inside_a_row():
    r = rows()[2]
    assert r["asset"] == "BlackRock, Inc. Common Stock (BLK) [ST]"
    assert r["status"] == "New"
    assert (r["transaction_date"], r["notification_date"]) == ("2025-01-02", "2025-01-03")


def test_open_ended_amount_on_the_next_line():
    r = rows()[3]
    assert r["asset"] == "U.S Treasury Bills [GS]"
    assert ptr.parse_amount(r["amount"]) == (1_000_000.0, 1_000_000.0)


def test_description_is_not_part_of_the_asset():
    r = rows()[4]
    assert r["asset"] == "Broadcom Inc. - Common Stock (AVGO) [OP]"
    assert r["description"].startswith("Put option")


def test_normalize_keeps_only_new_common_stock_trades():
    results = [ptr.normalize_row(r, FILING, {}) for r in rows()]
    trades = [t for t, _ in results if t]
    assert [(t["ticker"], t["side"], t["owner"], t["partial"]) for t in trades] == [
        ("MO", "sell", "SP", False), ("GOOGL", "sell", "SP", True), ("BLK", "buy", "self", False)]
    assert [why for t, why in results if not t] == [
        "not common stock: GS", "not common stock: OP", "amended row (may repeat an earlier filing)"]
    assert trades[0]["disclosure_date"] == "2025-04-04" and trades[0]["trade_date"] == "2025-03-27"
    assert trades[0]["asset"] == "Altria Group, Inc."


def test_ticker_forms_and_manual_map():
    base = {"owner": "", "type": "P", "transaction_date": "2025-01-02", "notification_date": "2025-01-03",
            "amount": "$1,001 - $15,000", "status": "New", "description": ""}
    trade, _ = ptr.normalize_row({**base, "asset": "Berkshire Hathaway Inc. New (BRK.B) [ST]"}, FILING, {})
    assert trade["ticker"] == "BRK-B"
    none, why = ptr.normalize_row({**base, "asset": "Example Holdings Common Stock [ST]"}, FILING, {})
    assert none is None and why == "no ticker in the asset line"
    trade, _ = ptr.normalize_row({**base, "asset": "Example Holdings Common Stock [ST]"}, FILING,
                                 {"example holdings common stock": "EXH"})
    assert trade["ticker"] == "EXH"
    none, why = ptr.normalize_row({**base, "transaction_date": "2025-05-01"}  | {"asset": "X (X) [ST]"}, FILING, {})
    assert why.startswith("trade date after")


def test_amounts():
    assert ptr.parse_amount("$1,001 - $15,000") == (1001.0, 15000.0)
    assert ptr.parse_amount("$1,140.00") == (1140.0, 1140.0)
    assert ptr.parse_amount("Over $50,000,000") == (50_000_000.0, 50_000_000.0)
    assert ptr.parse_amount("None") is None
