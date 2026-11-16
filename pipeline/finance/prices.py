"""Price sources. Used only in live mode, when a snapshot is being built.

Every source returns the same thing: JSON bytes
    {"ticker": ..., "source": ..., "prices": [["2024-01-02", 185.64], ...]}
with dividend- and split-adjusted closes, oldest first. Chaos and reference
runs never call a source; they read these bytes from the snapshot.
"""
from __future__ import annotations

import json
import os
import time
from typing import ClassVar, Protocol

from capitol_sdk import Fatal

BENCHMARK = "SPY"  # S&P 500 fund; its adjusted close includes dividends, like the stocks'


class PriceSource(Protocol):
    name: str

    def daily(self, ticker: str, start: str, end: str) -> list[list]: ...


class TiingoSource:
    """Primary source. Needs a free API key in TIINGO_API_KEY."""

    name = "tiingo"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def daily(self, ticker: str, start: str, end: str) -> list[list]:
        import requests

        response = requests.get(
            f"https://api.tiingo.com/tiingo/daily/{ticker.lower()}/prices",
            params={"startDate": start, "endDate": end, "format": "json"},
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return [[row["date"][:10], float(row["adjClose"])] for row in response.json()]


class YFinanceSource:
    """Fallback. Unofficial, can break without notice, has no delisted tickers."""

    name = "yfinance"
    _last_call: ClassVar[float] = 0.0

    def daily(self, ticker: str, start: str, end: str) -> list[list]:
        import logging

        import yfinance

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        wait = YFinanceSource._last_call + 1.0 - time.monotonic()  # at most one request a second
        if wait > 0:
            time.sleep(wait)
        YFinanceSource._last_call = time.monotonic()
        frame = yfinance.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if frame.empty or "Adj Close" not in frame.columns:
            return []
        closes = frame["Adj Close"].dropna()
        return [[day.date().isoformat(), float(value)] for day, value in closes.items()]


def sources() -> list[PriceSource]:
    key = os.environ.get("TIINGO_API_KEY", "")
    return ([TiingoSource(key)] if key else []) + [YFinanceSource()]


def download(ticker: str, start: str, end: str) -> bytes:
    """Try each source in order; the first one with data wins."""
    errors = []
    for source in sources():
        try:
            prices = source.daily(ticker, start, end)
        except Exception as err:  # noqa: BLE001 - any source error means "try the next source"
            errors.append(f"{source.name}: {type(err).__name__}: {err}")
            continue
        if prices:
            return json.dumps({"ticker": ticker, "source": source.name, "prices": prices},
                              sort_keys=True, separators=(",", ":")).encode()
        errors.append(f"{source.name}: no data")
    raise Fatal(f"no prices for {ticker} ({'; '.join(errors)})")
