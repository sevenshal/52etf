from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from pypdf import PdfReader

from ..core.analytics_database import ANALYTICS_DB_PATH
from ..core.duckdb_utils import connect_duckdb
from .hk_stock_base_data_config import HANG_SENG_REVIEW_RELEASE_DATES


logger = logging.getLogger(__name__)

INDEX_LIMITS = {
    "HSI": (50, 120),
    "HSCEI": (30, 80),
    "HSTECH": (25, 40),
}
DEFAULT_CODEX_PATH = os.getenv("HK_REVIEW_CODEX_PATH", "/home/ecs-user/.local/bin/codex")
DEFAULT_DISCOVERY_LOOKBACK_DAYS = int(os.getenv("HK_REVIEW_DISCOVERY_LOOKBACK_DAYS", "45"))
DEFAULT_CODEX_TIMEOUT_SECONDS = int(os.getenv("HK_REVIEW_CODEX_TIMEOUT_SECONDS", "900"))
PRESS_RELEASE_URL = (
    "https://www.hsi.com.hk/static/uploads/contents/en/news/pressRelease/"
    "{release_stamp}.pdf"
)


CODEX_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["snapshots"],
    "properties": {
        "snapshots": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "index_code",
                    "effective_date",
                    "reference_date",
                    "source_pages",
                    "constituents",
                ],
                "properties": {
                    "index_code": {"type": "string", "enum": list(INDEX_LIMITS)},
                    "effective_date": {"type": "string", "format": "date"},
                    "reference_date": {
                        "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]
                    },
                    "source_pages": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "constituents": {
                        "type": "array",
                        "minItems": 25,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "code",
                                "name",
                                "weight",
                                "free_float_factor",
                            ],
                            "properties": {
                                "code": {"type": "string", "pattern": "^[0-9]{1,5}$"},
                                "name": {"type": "string", "minLength": 1},
                                "weight": {"type": "number", "exclusiveMinimum": 0},
                                "free_float_factor": {
                                    "anyOf": [
                                        {"type": "number", "exclusiveMinimum": 0},
                                        {"type": "null"},
                                    ]
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


def _atomic_write_json(path: Path, payload: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _fridays_between(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor.weekday() != 4:
        cursor += timedelta(days=1)
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=7)


class HKIndexReviewAutomation:
    """Discover official review PDFs and let Codex produce guarded candidates."""

    def __init__(
        self,
        sync_service,
        cache_dir: Optional[str] = None,
        codex_path: Optional[str] = None,
        discovery_lookback_days: int = DEFAULT_DISCOVERY_LOOKBACK_DAYS,
        codex_timeout_seconds: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
    ):
        self.sync_service = sync_service
        self.cache_dir = Path(
            cache_dir
            or os.getenv(
                "HK_REVIEW_CACHE_DIR",
                "/var/lib/quant_robot/cache/hk_index_reviews",
            )
        )
        self.codex_path = str(codex_path or DEFAULT_CODEX_PATH)
        self.discovery_lookback_days = max(30, int(discovery_lookback_days))
        self.codex_timeout_seconds = max(60, int(codex_timeout_seconds))

    def run(self, as_of: Optional[date] = None) -> Dict:
        as_of_date = as_of or date.today()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        documents = self.discover_documents(as_of_date)
        imported_documents = self._imported_source_documents()
        pending = [
            item
            for item in documents
            if item["path"].name not in imported_documents
        ]
        completed = []
        errors = []
        for document in pending:
            try:
                completed.append(self._process_document(document, as_of_date))
            except Exception as exc:
                error = {
                    "document": document["path"].name,
                    "source_url": document["source_url"],
                    "error": str(exc),
                }
                errors.append(error)
                logger.exception(
                    "HK index review automation failed document=%s",
                    document["path"],
                )
        result = {
            "status": "FAILED" if errors else "SUCCESS",
            "discovered": len(documents),
            "pending": len(pending),
            "completed": completed,
            "errors": errors,
        }
        _atomic_write_json(self.cache_dir / "automation_last_run.json", result)
        if errors:
            raise RuntimeError(
                "HK index review automation rejected documents: "
                + "; ".join(f"{item['document']}: {item['error']}" for item in errors)
            )
        return result

    def discover_documents(self, as_of: date) -> List[Dict]:
        known_dates = {
            datetime.strptime(value, "%Y%m%d").date()
            for value in HANG_SENG_REVIEW_RELEASE_DATES
            if value <= as_of.strftime("%Y%m%d")
        }
        recent_start = as_of - timedelta(days=self.discovery_lookback_days)
        release_dates = sorted(known_dates | set(_fridays_between(recent_start, as_of)))
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = executor.map(
                lambda release_date: self._discover_release_document(
                    release_date,
                    release_date in known_dates,
                ),
                release_dates,
            )
            documents = [item for item in results if item]
        return sorted(documents, key=lambda item: item["path"].name)

    def _discover_release_document(
        self,
        release_date: date,
        is_known_date: bool,
    ) -> Optional[Dict]:
        output = self.cache_dir / f"{release_date:%Y%m%d}.pdf"
        metadata_path = output.with_suffix(".source.json")
        if output.exists() and output.stat().st_size > 10_000:
            metadata = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists()
                else {}
            )
            return {
                "path": output,
                "source_url": metadata.get("source_url"),
                "sha256": self._sha256(output),
            }
        timestamps = (
            ("T174500", "T180000", "T183000", "T163000", "T000000")
            if is_known_date
            else ("T174500", "T180000")
        )
        for timestamp in timestamps:
            source_url = PRESS_RELEASE_URL.format(
                release_stamp=f"{release_date:%Y%m%d}{timestamp}"
            )
            response = requests.get(
                source_url,
                headers={"User-Agent": "52ETF-HK-index-review-monitor/1.0"},
                timeout=30,
            )
            if response.status_code != 200 or not response.content.startswith(b"%PDF"):
                continue
            temporary = output.with_suffix(".pdf.tmp")
            temporary.write_bytes(response.content)
            if not self._is_index_review_pdf(temporary):
                temporary.unlink(missing_ok=True)
                continue
            temporary.replace(output)
            metadata = {
                "source_url": source_url,
                "sha256": self._sha256(output),
                "downloaded_at": datetime.now().isoformat(),
            }
            _atomic_write_json(metadata_path, metadata)
            return {
                "path": output,
                "source_url": source_url,
                "sha256": metadata["sha256"],
            }
        return None

    @staticmethod
    def _is_index_review_pdf(path: Path) -> bool:
        reader = PdfReader(str(path))
        leading_text = "\n".join(
            page.extract_text() or "" for page in reader.pages[:3]
        ).lower()
        return (
            "index review results" in leading_text
            and "hang seng index" in leading_text
        )

    def _process_document(self, document: Dict, as_of: date) -> Dict:
        pdf_path = document["path"]
        text_path = pdf_path.with_suffix(".txt")
        schema_path = pdf_path.with_suffix(".schema.json")
        candidate_path = pdf_path.with_suffix(".candidate.json")
        evidence_text = self._extract_text(pdf_path)
        text_path.write_text(evidence_text, encoding="utf-8")
        _atomic_write_json(schema_path, CODEX_OUTPUT_SCHEMA)
        candidate = self._run_codex(text_path, schema_path, candidate_path)
        manifest = self._prepare_and_validate_manifest(candidate, document, as_of)
        new_symbols = self._new_constituent_symbols(manifest)
        history = self._bootstrap_new_symbols(new_symbols, as_of)
        _atomic_write_json(candidate_path, manifest)
        imported = self.sync_service.import_weight_snapshot_manifest(str(candidate_path))
        return {
            "document": pdf_path.name,
            "sha256": document["sha256"],
            "snapshots": imported["snapshots"],
            "rows": imported["rows"],
            "new_symbols": new_symbols,
            "history": history,
        }

    @staticmethod
    def _extract_text(pdf_path: Path) -> str:
        sections = []
        for page_number, page in enumerate(PdfReader(str(pdf_path)).pages, start=1):
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                page_text = page.extract_text() or ""
            sections.append(
                f"\n===== PAGE {page_number} =====\n{page_text}"
            )
        return "".join(sections)

    def _run_codex(
        self,
        text_path: Path,
        schema_path: Path,
        candidate_path: Path,
    ) -> Dict:
        if not Path(self.codex_path).is_file():
            raise RuntimeError(f"Codex executable not found: {self.codex_path}")
        prompt = (
            "Read the official Hang Seng index review text file "
            f"{text_path.name}. Extract the complete post-review constituent lists and "
            "post-review weights for exactly HSI, HSCEI, and HSTECH from the appendices. "
            "Only use security rows under each appendix's 'Constituent List'. Read the "
            "rightmost 'After **' value from the 'Weighting (%)' columns; do not use the "
            "'Before' value, FAF, sector subtotal, constituent count, or share-class table. "
            "Use the document's stated effective date. Codes must contain digits only. "
            "Weights must be the published percentage weights and must total approximately "
            "100 for each index. Record the 1-based source page numbers. Never estimate, "
            "normalize, or invent missing rows. Return only the schema-conforming JSON."
        )
        completed = subprocess.run(
            [
                self.codex_path,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(candidate_path),
                "--cd",
                str(self.cache_dir),
                prompt,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.codex_timeout_seconds,
            check=False,
        )
        log_path = candidate_path.with_suffix(".codex.log")
        log_path.write_text(completed.stdout or "", encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"Codex exited with {completed.returncode}; see {log_path.name}"
            )
        try:
            return json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Codex returned invalid JSON: {exc}") from exc

    def _prepare_and_validate_manifest(
        self,
        candidate: Dict,
        document: Dict,
        as_of: date,
    ) -> Dict:
        snapshots = candidate.get("snapshots")
        if not isinstance(snapshots, list):
            raise ValueError("candidate has no snapshots array")
        by_index = {str(item.get("index_code") or "").upper(): item for item in snapshots}
        if set(by_index) != set(INDEX_LIMITS) or len(snapshots) != len(by_index):
            raise ValueError("candidate must contain exactly one HSI, HSCEI and HSTECH snapshot")
        latest_dates = self._latest_effective_dates()
        previous_codes = self._latest_constituent_codes()
        prepared = []
        effective_dates = set()
        for index_code, limits in INDEX_LIMITS.items():
            snapshot = dict(by_index[index_code])
            effective_date = datetime.strptime(
                str(snapshot["effective_date"]), "%Y-%m-%d"
            ).date()
            effective_dates.add(effective_date)
            if effective_date > as_of + timedelta(days=45):
                raise ValueError(f"{index_code} effective date is implausibly far in the future")
            if latest_dates.get(index_code) and effective_date <= latest_dates[index_code]:
                raise ValueError(
                    f"{index_code} effective date {effective_date} is not newer than "
                    f"{latest_dates[index_code]}"
                )
            constituents = snapshot.get("constituents") or []
            if not limits[0] <= len(constituents) <= limits[1]:
                raise ValueError(
                    f"{index_code} constituent count {len(constituents)} outside {limits}"
                )
            codes = []
            total = 0.0
            for item in constituents:
                raw_code = str(item.get("code") or "").strip()
                if not raw_code.isdigit():
                    raise ValueError(f"{index_code} invalid constituent code {raw_code!r}")
                code = f"{int(raw_code):05d}.HK"
                codes.append(code)
                total += float(item.get("weight") or 0)
                item["code"] = code
            if len(codes) != len(set(codes)):
                raise ValueError(f"{index_code} contains duplicate constituents")
            if not 99.5 <= total <= 100.5:
                raise ValueError(
                    f"{index_code} weights sum to {total:.4f}, refusing automatic import"
                )
            previous = previous_codes.get(index_code) or set()
            if previous:
                overlap = len(previous & set(codes)) / min(len(previous), len(codes))
                if overlap < 0.70:
                    raise ValueError(
                        f"{index_code} overlap with previous snapshot is only {overlap:.1%}"
                    )
            snapshot.update(
                {
                    "index_code": index_code,
                    "effective_date": effective_date.isoformat(),
                    "source_url": document["source_url"],
                    "source_document": document["path"].name,
                    "extraction_method": "official_pdf_codex_schema_validated",
                    "verified": True,
                }
            )
            prepared.append(snapshot)
        if len(effective_dates) != 1:
            raise ValueError("all three index snapshots must have the same effective date")
        return {
            "generated_at": datetime.now().isoformat(),
            "source_sha256": document["sha256"],
            "snapshots": prepared,
        }

    def _bootstrap_new_symbols(self, symbols: List[str], as_of: date) -> Dict:
        if not symbols:
            return {"symbols": 0, "completed": 0, "rows": 0, "errors": []}
        start_date = as_of - timedelta(days=900)
        result = self.sync_service.sync_symbols_history_yahoo(
            symbols,
            start_date=start_date,
            end_date=as_of,
        )
        if result.get("errors") or result.get("completed") != len(symbols):
            raise RuntimeError(f"new constituent history bootstrap incomplete: {result}")
        return result

    def _imported_source_documents(self) -> set:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            return {
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT source_document
                    FROM hk_index_weight_snapshot
                    WHERE source_document IS NOT NULL
                    """
                ).fetchall()
            }
        finally:
            connection.close()

    def _latest_effective_dates(self) -> Dict[str, date]:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            return dict(
                connection.execute(
                    """
                    SELECT index_code, MAX(effective_date)
                    FROM hk_index_weight_snapshot
                    GROUP BY index_code
                    """
                ).fetchall()
            )
        finally:
            connection.close()

    def _latest_constituent_codes(self) -> Dict[str, set]:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT index_code, con_code
                FROM hk_index_weight_snapshot current
                WHERE effective_date = (
                    SELECT MAX(previous.effective_date)
                    FROM hk_index_weight_snapshot previous
                    WHERE previous.index_code = current.index_code
                )
                """
            ).fetchall()
        finally:
            connection.close()
        result = {}
        for index_code, con_code in rows:
            result.setdefault(index_code, set()).add(con_code)
        return result

    def _new_constituent_symbols(self, manifest: Dict) -> List[str]:
        previous = self._latest_constituent_codes()
        new_symbols = set()
        for snapshot in manifest["snapshots"]:
            current = {item["code"] for item in snapshot["constituents"]}
            new_symbols.update(current - previous.get(snapshot["index_code"], set()))
        return sorted(new_symbols)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
