# Data notes (Phase 0)

Checked on 2026-11-12 with `tools/phase0/inspect_pdfs.py`. **Result: go.**

## House index

- URL: `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<year>FD.zip`
- Each ZIP holds `<year>FD.txt` (tab-separated) and `<year>FD.xml` with the same rows.
- Fields: `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID`. Dates are `M/D/YYYY`.
- `FilingType = P` marks a Periodic Transaction Report (PTR).

| Year | Rows | PTRs | PTR filers | Digital IDs (8 digits, `20...`) | Paper IDs (7 digits, `82...`, `91...`) |
| --- | --- | --- | --- | --- | --- |
| 2024 | 2,302 | 451 | 99 | 394 | 57 |
| 2025 | 2,977 | 515 | 108 | 449 | 66 |

So two years give **966 PTRs**, about 87% digital by id. This is smaller than the "4,000 children" example I first planned for; the 10,000-task test in Phase 4 still covers scale.

- PDF URL: `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<year>/<DocID>.pdf`
- There is no `robots.txt` (404).

## Use restrictions

The search page states it is unlawful to use the reports for: (A) any unlawful purpose; (B) any commercial purpose, except news media informing the public; (C) setting a person's credit rating; (D) asking for money for any political, charitable or other purpose (5 U.S.C. app. § 105(c)). Some personal details are redacted.

This project is non-commercial and makes no claim about any individual. We fetch at most one request every 2 seconds with a descriptive User-Agent, and all tests replay from a local snapshot.

## Sample of 20 PTRs

Random sample (seed 20261112), one filing per filer, both years.

- **19 of 20 are digital and have a text layer (95%).** The one scanned file has a 7-digit id. Gate (60%) passed.
- The id length predicts scanned files, but the parser must still check for a text layer.
- 152 trade rows in the sample; filings have 1 to 56 rows.

### Layout

One trade is one main line, then continuation lines:

```
SP Microsoft Corporation - Common P 09/09/2024 09/12/2024 $1,001 - $15,000
Stock (MSFT) [ST]
F S : New
S O : Trust One
```

- Main line: optional owner, asset name, type, transaction date, notification date, amount.
- The asset name often wraps, so `(TICKER) [CODE]` can be on the next line.
- Large amount bands wrap too: `$15,001 -` on the main line, `$50,000` on the next.
- `pdfplumber.extract_tables()` is **not** reliable here: some rows come back as one merged cell. Line-based text parsing with `extract_text()` is consistent. Phase 6 uses text lines.
- Section headings come out as broken letters with NUL bytes (a font issue). We ignore them.

### Values seen

| Field | Values |
| --- | --- |
| Owner | blank (self) 96, `SP` 38, `JT` 17, `DC` 1 |
| Type | `P` 70, `S` 62, `S (partial)` 20; `E` (exchange) exists but was not in the sample |
| Asset code | `ST` 144, `GS` 3, `OP` 2, `CT` 2, `HN` 1 |
| Amount | `$1,001 - $15,000` 114, `$15,001 - $50,000` 27, `$50,001 - $100,000` 6, `$100,001 - $250,000` 3, `$250,001 - $500,000` 1, one exact amount (`$1,140.00`) |
| Ticker | Printed as `(TICKER)` before `[ST]` for almost all stocks. No fuzzy matching needed. |

Dates: the PDF gives the transaction date and the notification date. The **disclosure date** for the event study is `FilingDate` from the index.

## Prices

- `yfinance` 1.8.0 returned 252 daily rows for 2024 with adjusted close for AAPL, MSFT, NVDA, MO, AGIO, `^GSPC` and SPY.
- Delisted tickers (SIVB, ATVI) return nothing. Trades in them will be counted as "missing prices".
- Tiingo is still the planned primary source but needs a free API key. Not checked yet.

## Reference data for the research checks

Fetched once by `tools/fetch_research_data.py` and frozen in `snapshots/research/`. The analysis reads only these files.

| File | Source | Notes |
| --- | --- | --- |
| `committees.json` | [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators), `committee-membership-current.yaml` | The file only holds "now", so one old commit is pinned per Congress: `c22da268` (28 June 2024, 118th) and `65259e46` (17 June 2025, 119th). Full House committees only. |
| `members.json` | same repository, `legislators-current.yaml` and `legislators-historical.yaml` | Member id, last name and House terms (state, district, dates) that touch 2023 to 2026. |
| `benchmarks.json` | same price source as the run (`yfinance`) | RSP (equal-weight S&P 500), QQQ (Nasdaq-100) and 11 sector funds, adjusted closes. XLC starts in 2018 and XLRE in late 2015. |
| `sectors.json` | Yahoo Finance through `yfinance` (`.info`), one request every 0.4 s | Sector, industry, head-office state and country, company size. **Labels are those of the fetch date**, not of the trade date. |

A filing is linked to a member by its seat (`SC03`) and filing date. The last name is then compared as a check; when it differs, the trade is kept but gets no committee.
