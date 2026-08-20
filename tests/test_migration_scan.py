import tempfile
import unittest
from pathlib import Path

import migration_scan


class MigrationScanTests(unittest.TestCase):
    def test_scan_detects_legacy_dependency_client_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("azure-ai-inference>=1.0.0b9\n", encoding="utf-8")
            (root / "client.py").write_text(
                "client = ChatCompletionsClient(endpoint + '/models?api-version=2024-05-01-preview')\n",
                encoding="utf-8",
            )

            findings = migration_scan.scan([root])

        self.assertEqual(
            {finding.rule_id for finding in findings},
            {"AOAI001", "AOAI002", "AOAI003", "AOAI004"},
        )

    def test_scan_ignores_dependency_and_custom_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ignored in ("node_modules", "generated"):
                path = root / ignored
                path.mkdir()
                (path / "client.py").write_text("ChatCompletionsClient()\n", encoding="utf-8")

            findings = migration_scan.scan([root], excludes=["generated"])

        self.assertEqual(findings, [])

    def test_sarif_report_contains_source_location(self) -> None:
        finding = migration_scan.Finding(
            rule_id="AOAI005",
            message="Legacy AzureOpenAI client usage",
            path="src/client.py",
            line=9,
            column=4,
            excerpt="AzureOpenAI(",
        )

        report = migration_scan.sarif_report([finding])
        result = report["runs"][0]["results"][0]

        self.assertEqual(result["ruleId"], "AOAI005")
        self.assertEqual(
            result["locations"][0]["physicalLocation"]["region"]["startLine"],
            9,
        )


if __name__ == "__main__":
    unittest.main()