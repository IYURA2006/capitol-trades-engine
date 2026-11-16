"""Phase 0 check: can pdfplumber read a sample of House PTR PDFs?

Usage: python inspect_pdfs.py <pdf_dir> [docid_to_dump]
"""
import pathlib
import re
import sys

import pdfplumber

TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]")
ROW = re.compile(
    r"\b(P|S|E|S \(partial\))\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\$[\d,]+(?:\s*-\s*\$[\d,]+)?|Over \$[\d,]+)"
)


def main() -> None:
    pdf_dir = pathlib.Path(sys.argv[1])
    dump = sys.argv[2] if len(sys.argv) > 2 else None
    print("docid\tpages\tchars\ttables\trows\ttickers\ttypes\tkind")
    for path in sorted(pdf_dir.glob("*.pdf")):
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            tables = sum(len(p.extract_tables()) for p in pdf.pages)
            pages = len(pdf.pages)
        rows = ROW.findall(text)
        tickers = TICKER.findall(text)
        types = sorted({t for _, t in tickers} | set(re.findall(r"\[([A-Z]{2})\]", text)))
        kind = "digital" if len(text) > 200 else "scanned"
        print(f"{path.stem}\t{pages}\t{len(text)}\t{tables}\t{len(rows)}\t{len(tickers)}\t{','.join(types)}\t{kind}")
        if dump == path.stem:
            print("-----\n" + text + "\n-----")


if __name__ == "__main__":
    main()
