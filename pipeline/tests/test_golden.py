"""Golden-file tests on real filings from the committed sample snapshot.

For 15 filings, the expected rows in pipeline/tests/golden/<doc_id>.json
were checked against the PDF text. The test runs the real path:
PDF bytes -> pdfplumber -> parse_ptr_text -> normalize_row.

Regenerate after an intended parser change with:
    GOLDEN_UPDATE=1 python -m pytest pipeline/tests/test_golden.py
and read the diff before committing it.
"""
import json
import os
from pathlib import Path

import pytest

from capitol_sdk import run_local
from capitol_sdk.http import SnapshotHttp
from pipeline import ptr
from pipeline.tasks import filings

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "golden"
DOCS = [d for d in filings.SAMPLE_DOCS if d not in ("8221237", "20026726", "20029084", "20018054", "20033581")]


@pytest.fixture(scope="module")
def http():
    return SnapshotHttp(ROOT / "snapshots", "sample")


@pytest.fixture(scope="module")
def index(http):
    rows = []
    for year in (2024, 2025):
        rows += ptr.parse_index(http.get(ptr.INDEX_URL.format(year=year)), year)
    return {f["doc_id"]: f for f in rows}


def test_fifteen_filings_are_covered():
    assert len(DOCS) == 15


@pytest.mark.parametrize("doc_id", DOCS)
def test_filing_matches_golden_file(doc_id, http, index):
    filing = index[doc_id]
    text = filings.pdf_text(http.get(ptr.PDF_URL.format(year=filing["year"], doc_id=doc_id)))
    actual = []
    for row in ptr.parse_ptr_text(text):
        trade, reason = ptr.normalize_row(row, filing, {})
        actual.append(trade if trade else {"rejected": reason, "asset": row["asset"]})
    path = GOLDEN / f"{doc_id}.json"
    if os.environ.get("GOLDEN_UPDATE"):
        path.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert actual == json.loads(path.read_text())


def test_index_marks_966_ptrs(index):
    assert len(index) == 966


def test_whole_sample_pipeline(tmp_path, http):
    result = run_local("capitol_sample", "sample", seed=7, blob_dir=tmp_path, http=http)
    assert result.run_state == "SUCCEEDED"
    assert result.tasks["parse_filing/8221237"] == "DEAD_LETTER"  # the scanned filing
    assert result.tasks["normalize_trades/all"] == "SUCCEEDED"  # the join tolerates it
    summary = result.records["trades_summary\nall"]
    assert (summary["filings_found"], summary["filings_parsed"], summary["filings_skipped"]) == (20, 19, 1)
    assert (summary["rows"], summary["trades"]) == (152, 144)
    assert summary["rejected"] == {"not common stock: CT": 2, "not common stock: GS": 3,
                                   "not common stock: HN": 1, "not common stock: OP": 2}
    study = result.records["study_summary\nall"]
    assert study["studied"] + sum(study["skipped"].values()) == 144
    aggregates = [v for k, v in result.records.items() if k.startswith("aggregate\n")]
    assert len(aggregates) == 24 and all(a["mean"] is not None for a in aggregates if a["n"] >= 2)
    # Same seed, same snapshot: the same bytes.
    again = run_local("capitol_sample", "sample", seed=7, blob_dir=tmp_path, http=http)
    assert again.to_json() == result.to_json()
