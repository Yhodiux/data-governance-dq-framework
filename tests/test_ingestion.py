from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.ingestion import IngestionConfig, run_ingestion


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.raw = self.root / "raw"
        self.results = self.root / "results" / "ingestion"
        self.manifest = self.root / "dataset_manifest.yaml"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def checksum(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest().upper()

    def write_source(self, file_name: str, content: bytes) -> None:
        (self.source / file_name).write_bytes(content)

    def write_manifest(self, entries: list[dict[str, object]]) -> None:
        self.manifest.write_text(
            yaml.safe_dump({"dataset": {"files": entries}}, sort_keys=False),
            encoding="utf-8",
        )

    def entry(
        self,
        file_name: str,
        content: bytes,
        *,
        sha256: str | None = None,
        record_count: int | None = None,
    ) -> dict[str, object]:
        measured_count = max(0, len(content.splitlines()) - 1)
        return {
            "name": file_name,
            "expected_record_count": (
                measured_count if record_count is None else record_count
            ),
            "sha256": self.checksum(content) if sha256 is None else sha256,
        }

    def run_test_ingestion(self) -> dict[str, object]:
        return run_ingestion(
            IngestionConfig(
                source_path=self.source,
                manifest_path=self.manifest,
                raw_path=self.raw,
                results_path=self.results,
            )
        )

    def assert_execution_record(self, execution: dict[str, object]) -> None:
        records = list(self.results.glob("*.json"))
        self.assertEqual(len(records), 1)
        stored = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(stored["run_id"], execution["run_id"])
        self.assertEqual(stored["status"], execution["status"])

    def test_successful_validation_and_publication(self) -> None:
        account = b'id;value\n1;"one"\n2;"two"\n'
        client = b"id;value\n10;alpha\n"
        self.write_source("account.asc", account)
        self.write_source("client.asc", client)
        self.write_manifest(
            [self.entry("account.asc", account), self.entry("client.asc", client)]
        )

        execution = self.run_test_ingestion()

        self.assertEqual(execution["status"], "SUCCESS")
        self.assertEqual((self.raw / "account.asc").read_bytes(), account)
        self.assertEqual((self.raw / "client.asc").read_bytes(), client)
        self.assertTrue(
            all(item["validation_status"] == "PASSED" for item in execution["files"])
        )
        self.assert_execution_record(execution)

    def test_missing_source_file_fails_without_raw_publication(self) -> None:
        content = b"id\n1\n"
        self.write_manifest([self.entry("missing.asc", content)])

        execution = self.run_test_ingestion()

        self.assertEqual(execution["status"], "FAILED")
        self.assertFalse(self.raw.exists())
        self.assertEqual(execution["files"][0]["message"], "Source file is missing")
        self.assert_execution_record(execution)

    def test_sha256_mismatch_fails_without_raw_publication(self) -> None:
        content = b"id\n1\n"
        self.write_source("account.asc", content)
        self.write_manifest([self.entry("account.asc", content, sha256="0" * 64)])

        execution = self.run_test_ingestion()

        self.assertEqual(execution["status"], "FAILED")
        self.assertFalse(self.raw.exists())
        self.assertIn("SHA-256 mismatch", execution["files"][0]["message"])
        self.assert_execution_record(execution)

    def test_record_count_mismatch_fails_without_raw_publication(self) -> None:
        content = b"id\n1\n"
        self.write_source("account.asc", content)
        self.write_manifest([self.entry("account.asc", content, record_count=2)])

        execution = self.run_test_ingestion()

        self.assertEqual(execution["status"], "FAILED")
        self.assertFalse(self.raw.exists())
        self.assertIn("Record-count mismatch", execution["files"][0]["message"])
        self.assert_execution_record(execution)

    def test_copy_failure_preserves_previous_raw_without_partial_dataset(self) -> None:
        first = b"id\n1\n"
        second = b"id\n2\n"
        self.write_source("first.asc", first)
        self.write_source("second.asc", second)
        self.write_manifest(
            [self.entry("first.asc", first), self.entry("second.asc", second)]
        )
        self.raw.mkdir()
        (self.raw / "previous.asc").write_bytes(b"previous raw dataset\n")

        real_copy2 = shutil.copy2
        calls = 0

        def fail_second_copy(source: Path, destination: Path) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated publication failure")
            return real_copy2(source, destination)

        with patch("src.ingestion.core.shutil.copy2", side_effect=fail_second_copy):
            execution = self.run_test_ingestion()

        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual((self.raw / "previous.asc").read_bytes(), b"previous raw dataset\n")
        self.assertFalse((self.raw / "first.asc").exists())
        self.assertFalse((self.raw / "second.asc").exists())
        self.assertEqual(list(self.raw.parent.glob(".raw-staging-*")), [])
        self.assert_execution_record(execution)


if __name__ == "__main__":
    unittest.main()
