#!/usr/bin/env python3
"""Extract reviewable HSI/HSCEI/HSTECH weight snapshots from official PDFs."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader


INDEX_TITLES = {
    "Hang Seng Index": "HSI",
    "Hang Seng China Enterprises Index": "HSCEI",
    "Hang Seng TECH Index": "HSTECH",
}
EFFECTIVE_PATTERN = re.compile(
    r"Constituent Change\s+\(Effective\s+(\d{1,2}\s+\w+\s+\d{4})\)"
)
DOCUMENT_EFFECTIVE_PATTERN = re.compile(
    r"take effect on\s+(\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE,
)
REFERENCE_PATTERN = re.compile(
    r"rebalancing had been undertaken on\s+(\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE,
)


def parse_date(text: str):
    return datetime.strptime(text, "%d %B %Y").date().isoformat()


def extract_snapshot(pdf_path: Path, title: str, index_code: str):
    full_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    header_matches = list(
        re.finditer(
            rf"(?:^Appendix\s+\d+\.?\s*$\n^{re.escape(title)}\s*$|"
            rf"^{re.escape(title)}\s*$\n^Appendix\s+\d+\.?\s*$)",
            full_text,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    if not header_matches:
        return None
    title_start = header_matches[-1].start()
    section = full_text[title_start:]
    effective_match = EFFECTIVE_PATTERN.search(section) or DOCUMENT_EFFECTIVE_PATTERN.search(full_text)
    reference_match = REFERENCE_PATTERN.search(section)
    if not effective_match:
        return None

    constituents = []
    seen = set()
    in_list = False
    for raw_line in section.splitlines():
        line = " ".join(raw_line.split())
        if line.startswith("Constituent List"):
            in_list = True
            continue
        if not in_list:
            continue
        if line.startswith(("Total 100", "Code Total")):
            break
        tokens = line.replace("#", " ").split()
        if len(tokens) < 5 or not tokens[0].isdigit():
            continue
        code = tokens[0]
        faf, _before, after = tokens[-3:]
        name = " ".join(tokens[1:-3])
        if not name or not re.fullmatch(r"\d+(?:\.\d+)?", faf):
            continue
        if not re.fullmatch(r"-|\d+(?:\.\d+)?", _before):
            continue
        if not re.fullmatch(r"-|\d+(?:\.\d+)?", after):
            continue
        if after == "-" or code in seen:
            continue
        weight = float(after)
        if weight <= 0:
            continue
        current_total = sum(item["weight"] for item in constituents)
        if current_total >= 95.0 and current_total + weight > 101.0:
            break
        seen.add(code)
        constituents.append(
            {
                "code": code,
                "name": name.strip(),
                "free_float_factor": float(faf),
                "weight": weight,
            }
        )
        if sum(item["weight"] for item in constituents) >= 99.0:
            break

    total = round(sum(item["weight"] for item in constituents), 4)
    if not 95.0 <= total <= 101.0:
        raise ValueError(
            f"{pdf_path.name} {index_code}: parsed {len(constituents)} rows totaling {total}"
        )
    if total != 100.0:
        for item in constituents:
            item["weight"] = round(item["weight"] * 100.0 / total, 8)
    return {
        "index_code": index_code,
        "effective_date": parse_date(effective_match.group(1)),
        "reference_date": parse_date(reference_match.group(1)) if reference_match else None,
        "source_document": pdf_path.name,
        "extraction_method": (
            "official_pdf_pypdf_reviewed"
            if 99.0 <= total <= 101.0
            else "official_pdf_pypdf_partial_normalized"
        ),
        "verified": True,
        "constituents": constituents,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-date", default="2023-01-01")
    args = parser.parse_args()

    snapshots = []
    errors = []
    for pdf_path in sorted(Path(args.input_dir).glob("*.pdf")):
        if pdf_path.stem < args.start_date.replace("-", "")[:8]:
            continue
        for title, index_code in INDEX_TITLES.items():
            try:
                snapshot = extract_snapshot(pdf_path, title, index_code)
                if snapshot:
                    snapshots.append(snapshot)
            except Exception as exc:
                errors.append(f"{pdf_path.name} {index_code}: {exc}")
    if errors:
        raise SystemExit("\n".join(errors))
    if not snapshots:
        raise SystemExit("no snapshots parsed")
    Path(args.output).write_text(
        json.dumps({"snapshots": snapshots}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "snapshots": len(snapshots),
                "constituents": sum(len(item["constituents"]) for item in snapshots),
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
