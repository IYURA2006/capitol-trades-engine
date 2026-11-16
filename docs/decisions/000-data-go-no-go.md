# 000: Data go / no-go

**Decision.** Go. Use House PTRs for 2024 and 2025, and parse them from text lines, not tables.

**Evidence.** In a random sample of 20 filings from 20 filers, 19 had a text layer and showed the ticker in the asset line. The index marks PTRs clearly and the PDF URL is predictable.

**Alternative.** `pdfplumber` table extraction, as the plan first said. Rejected: on these files it sometimes merges a whole row into one cell, so results change from row to row. The text lines always have the same order: owner, asset, type, two dates, amount.

**Why.** A regex over lines is simple to explain and easy to test with golden files. Scanned filings (about 13% by id) go to a skipped list with a count; there is no OCR.

**Open point.** Tiingo needs a free API key. `yfinance` works today as the fallback, but has no data for delisted tickers.
