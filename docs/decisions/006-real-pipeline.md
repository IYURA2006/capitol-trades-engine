# 006: Real pipeline and snapshots

**Decision.** Tasks reach the outside world only through `ctx.http`. In live mode it downloads (one request every 2 seconds per host) and keeps every answer in the blob store. `tools/make_snapshot.py` freezes those answers into `snapshots/<id>/`. Every other run reads the snapshot and never touches a website.

**Alternative.** Let tasks call `requests` directly and mock the network in tests.

**Why.** "Chaos run equals reference" is only true if both see the same inputs. Live websites change, and 100 chaos runs against the Clerk's site would be abusive. With a snapshot a task is a pure function of its payload and the frozen files.

**Parsing.** PTRs are parsed from text lines with one regular expression per row, not with table extraction (see decision 000). Only common stock (`[ST]`) is kept. Every dropped row becomes a `rejected` record with a reason, so nothing disappears silently.

**Data flow.** Price series are large, so they go to the blob store and only their SHA-256 travels through the coordinator.

**Not done.** No OCR for scanned filings; amended rows are dropped to avoid counting a trade twice.
