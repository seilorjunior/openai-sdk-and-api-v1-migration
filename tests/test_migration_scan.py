import json
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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

    def test_scan_detects_legacy_client_and_deployment_endpoint_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client.py").write_text(
                "client = AzureOpenAI()\nurl = '/OPENAI/DEPLOYMENTS/chat/completions'\n",
                encoding="utf-8",
            )

            findings = migration_scan.scan([root])

        self.assertEqual([finding.rule_id for finding in findings], ["AOAI005", "AOAI006"])

    def test_scan_skips_non_text_and_invalid_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.bin").write_bytes(b"ChatCompletionsClient")
            (root / "broken.py").write_bytes(b"\xff\xfe\xfa")

            findings = migration_scan.scan([root])

        self.assertEqual(findings, [])

    def test_scan_results_and_json_counts_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z.py").write_text("AzureOpenAI()\n", encoding="utf-8")
            (root / "a.py").write_text("ChatCompletionsClient()\nAzureOpenAI()\n", encoding="utf-8")

            findings = migration_scan.scan([root])
            report = migration_scan.json_report(findings)

        self.assertEqual([finding.path for finding in findings], ["a.py", "a.py", "z.py"])
        self.assertEqual(report["finding_count"], 3)
        self.assertEqual(report["counts_by_rule"], {"AOAI002": 1, "AOAI005": 2})

    @patch("migration_scan.parse_args")
    def test_main_returns_two_for_missing_root(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            roots=[Path("does-not-exist")],
            exclude=[],
            format="json",
            output=None,
            fail_on_findings=False,
        )

        with patch("sys.stderr", new_callable=StringIO) as error:
            exit_code = migration_scan.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(error.getvalue())["error"], "roots_not_found")

    @patch("migration_scan.parse_args")
    def test_main_writes_json_and_fails_when_findings_are_required(self, parse_args) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_path = root / "report.json"
            (root / "client.py").write_text("AzureOpenAI()\n", encoding="utf-8")
            parse_args.return_value = Namespace(
                roots=[root],
                exclude=[],
                format="json",
                output=output_path,
                fail_on_findings=True,
            )

            with patch("sys.stdout", new_callable=StringIO):
                exit_code = migration_scan.main()

            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["counts_by_rule"], {"AOAI005": 1})


if __name__ == "__main__":
    unittest.main()