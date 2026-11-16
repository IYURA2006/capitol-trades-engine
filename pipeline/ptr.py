"""Pure functions for House Periodic Transaction Reports (PTRs).

No engine, no files, no network: text in, plain dicts out. That keeps them
easy to test with golden files. Layout notes are in docs/data-notes.md.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

# One trade starts with a line like:
#   SP Microsoft Corporation - Common P 09/09/2024 09/12/2024 $1,001 - $15,000
_ROW = re.compile(
    r"^(?:(?P<owner>SP|JT|DC)\s+)?"
    r"(?P<asset>.*?)\s*"
    r"(?<!\S)(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<tx>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notified>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>.+)$"
)
_STATUS = re.compile(r"^F S: (?P<status>\w+)")
_DETAIL = re.compile(r"^(?:S O|D|L|C): ")
_HEADER = re.compile(r"^(ID Owner Asset|Type Date Gains|\$200\?$)")
_END = re.compile(r"^(\* For the complete list|I V D|I P O|C S$|I CERTIFY)")
_MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
_TRAILING_MONEY = re.compile(r"\s*(\$[\d,]+(?:\.\d+)?)$")
_TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-/]{0,7})\)\s*\[([A-Z]{2})\]\s*$")
_ASSET_CODE = re.compile(r"\[([A-Z]{2})\]\s*$")


def parse_index(zip_bytes: bytes, year: int) -> list[dict]:
    """PTR rows (FilingType P) from a yearly index ZIP, sorted by DocID."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        text = archive.read(f"{year}FD.txt").decode("utf-8-sig", "replace")
    filings = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        doc_id = (row.get("DocID") or "").strip()
        if (row.get("FilingType") or "").strip() != "P" or not doc_id.isdigit():
            continue
        filings.append({
            "doc_id": doc_id,
            "year": int(row["Year"]),
            "last": row["Last"].strip(),
            "first": row["First"].strip(),
            "state_district": row["StateDst"].strip(),
            "filing_date": _iso(row["FilingDate"].strip(), "%m/%d/%Y"),
        })
    return sorted(filings, key=lambda f: f["doc_id"])


def _iso(text: str, fmt: str) -> str:
    return datetime.strptime(text, fmt).date().isoformat()


def clean_lines(text: str) -> list[str]:
    """The PDF's section labels use a font that comes out as NUL bytes
    ("F\\0\\0 S\\0\\0: New"). Remove them and squeeze spaces, so labels become
    "F S: New"."""
    lines = []
    for raw in text.replace("\x00", "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def parse_ptr_text(text: str) -> list[dict]:
    """Raw trade rows from the text of one PTR, in document order."""
    rows: list[dict] = []
    current: dict | None = None
    in_details = False  # after "F S:", lines belong to details, not the asset name

    for line in clean_lines(text):
        if _END.match(line):
            current = None
            continue
        if _HEADER.match(line):
            continue  # a page break can repeat the table header inside a row
        match = _ROW.match(line)
        if match:
            current = {
                "owner": match["owner"] or "",
                "asset": match["asset"].strip(),
                "type": match["type"],
                "transaction_date": _iso(match["tx"], "%m/%d/%Y"),
                "notification_date": _iso(match["notified"], "%m/%d/%Y"),
                "amount": match["amount"].strip(),
                "status": "",
                "description": "",
            }
            rows.append(current)
            in_details = False
            continue
        if current is None:
            continue
        status = _STATUS.match(line)
        if status:
            current["status"] = status["status"]
            in_details = True
        elif _DETAIL.match(line):
            in_details = True
            if line.startswith("D: "):
                current["description"] = line[3:]
        elif not in_details:
            # A wrapped line: more of the asset name, and sometimes the upper
            # end of the amount ("$50,000") at the end of the line.
            money = _TRAILING_MONEY.search(line)
            if money and _amount_is_open(current["amount"]):
                current["amount"] += " " + money.group(1)
                line = line[: money.start()].strip()
            if line:
                current["asset"] = (current["asset"] + " " + line).strip()
    return rows


def _amount_is_open(amount: str) -> bool:
    """True when the amount on the main line is not finished: "$15,001 -"
    waits for its upper end, "Spouse/DC Over" waits for its number."""
    amount = amount.rstrip()
    return amount.endswith(("-", "Over")) or not _MONEY.search(amount)


def parse_amount(text: str) -> tuple[float, float] | None:
    """("$1,001 - $15,000") -> (1001.0, 15000.0). An exact amount gives low = high.
    "Over $50,000,000" has no upper end, so high = low."""
    values = [float(m.replace("$", "").replace(",", "")) for m in _MONEY.findall(text)]
    if not values:
        return None
    return (values[0], values[-1]) if len(values) > 1 else (values[0], values[0])


def normalize_row(row: dict, filing: dict, ticker_map: dict[str, str]) -> tuple[dict | None, str]:
    """Turn one raw row into a clean trade, or say why it is rejected.

    Returns (trade, "") or (None, reason). Only common stock is kept.
    """
    asset = row["asset"]
    code = _ASSET_CODE.search(asset)
    if not code:
        return None, "no asset type code"
    if code.group(1) != "ST":
        return None, f"not common stock: {code.group(1)}"
    if row["status"] == "Amended":
        return None, "amended row (may repeat an earlier filing)"
    if row["type"] == "E":
        return None, "exchange, not a buy or sell"

    with_ticker = _TICKER.search(asset)
    name = _ASSET_CODE.sub("", asset).strip()
    if with_ticker:
        ticker = with_ticker.group(1)
        name = asset[: with_ticker.start()].strip()
    else:
        ticker = ticker_map.get(name.lower(), "")
        if not ticker:
            return None, "no ticker in the asset line"

    amount = parse_amount(row["amount"])
    if amount is None:
        return None, "amount not readable"
    trade_date = date.fromisoformat(row["transaction_date"])
    disclosed = date.fromisoformat(filing["filing_date"])
    if trade_date > disclosed or trade_date.year < 2012:
        return None, "trade date after the filing date or before 2012"

    return {
        "doc_id": filing["doc_id"],
        "filer": f"{filing['first']} {filing['last']}".strip(),
        "state_district": filing["state_district"],
        "owner": row["owner"] or "self",
        "ticker": ticker.replace("/", "-").replace(".", "-"),  # BRK.B and BRK/B -> BRK-B
        "asset": name,
        "side": "buy" if row["type"] == "P" else "sell",
        "partial": row["type"] == "S (partial)",
        "amount_low": amount[0],
        "amount_high": amount[1],
        "trade_date": row["transaction_date"],
        "notification_date": row["notification_date"],
        "disclosure_date": filing["filing_date"],
    }, ""
