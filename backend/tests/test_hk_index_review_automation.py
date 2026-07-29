import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.robot.hk_index_review_automation import HKIndexReviewAutomation


def _constituents(count):
    return [
        {
            "code": str(1000 + index),
            "name": f"Company {index}",
            "weight": 100.0 / count,
            "free_float_factor": 1.0,
        }
        for index in range(count)
    ]


def _candidate(effective_date="2026-09-07"):
    return {
        "snapshots": [
            {
                "index_code": "HSI",
                "effective_date": effective_date,
                "reference_date": "2026-06-30",
                "source_pages": [20, 21],
                "constituents": _constituents(50),
            },
            {
                "index_code": "HSCEI",
                "effective_date": effective_date,
                "reference_date": "2026-06-30",
                "source_pages": [22, 23],
                "constituents": _constituents(30),
            },
            {
                "index_code": "HSTECH",
                "effective_date": effective_date,
                "reference_date": "2026-06-30",
                "source_pages": [24],
                "constituents": _constituents(30),
            },
        ]
    }


class HKIndexReviewAutomationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.automation = HKIndexReviewAutomation(
            sync_service=object(),
            cache_dir=self.temporary.name,
            codex_path="/bin/false",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_candidate_is_normalized_and_source_is_forced(self):
        candidate = _candidate()
        document = {
            "path": Path(self.temporary.name) / "20260821.pdf",
            "source_url": "https://www.hsi.com.hk/official.pdf",
            "sha256": "abc123",
        }
        with (
            patch.object(
                self.automation,
                "_latest_effective_dates",
                return_value={
                    "HSI": date(2026, 6, 8),
                    "HSCEI": date(2026, 6, 8),
                    "HSTECH": date(2026, 6, 8),
                },
            ),
            patch.object(
                self.automation,
                "_latest_constituent_codes",
                return_value={},
            ),
        ):
            manifest = self.automation._prepare_and_validate_manifest(
                candidate,
                document,
                as_of=date(2026, 8, 21),
            )

        self.assertEqual(3, len(manifest["snapshots"]))
        self.assertEqual(
            "01000.HK",
            manifest["snapshots"][0]["constituents"][0]["code"],
        )
        self.assertEqual(
            "official_pdf_codex_schema_validated",
            manifest["snapshots"][0]["extraction_method"],
        )
        self.assertEqual(
            "https://www.hsi.com.hk/official.pdf",
            manifest["snapshots"][0]["source_url"],
        )

    def test_rejects_incomplete_weight_total(self):
        candidate = _candidate()
        candidate["snapshots"][0]["constituents"][0]["weight"] = 0.01
        with (
            patch.object(self.automation, "_latest_effective_dates", return_value={}),
            patch.object(self.automation, "_latest_constituent_codes", return_value={}),
            self.assertRaisesRegex(ValueError, "weights sum"),
        ):
            self.automation._prepare_and_validate_manifest(
                candidate,
                {
                    "path": Path(self.temporary.name) / "20260821.pdf",
                    "source_url": "https://www.hsi.com.hk/official.pdf",
                    "sha256": "abc123",
                },
                as_of=date(2026, 8, 21),
            )

    def test_existing_source_document_is_not_sent_to_codex(self):
        document = {
            "path": Path(self.temporary.name) / "20260522.pdf",
            "source_url": "https://www.hsi.com.hk/official.pdf",
            "sha256": "abc123",
        }
        with (
            patch.object(self.automation, "discover_documents", return_value=[document]),
            patch.object(
                self.automation,
                "_imported_source_documents",
                return_value={"20260522.pdf"},
            ),
            patch.object(self.automation, "_process_document") as process_document,
        ):
            result = self.automation.run(as_of=date(2026, 7, 29))

        self.assertEqual(0, result["pending"])
        process_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
