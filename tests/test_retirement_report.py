import json
import tempfile
import unittest
from argparse import Namespace
from datetime import timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import retirement_report


class RetirementReportTests(unittest.TestCase):
    def test_build_query_contains_every_window_and_correlation_join(self) -> None:
        query = retirement_report.build_query()

        for days in retirement_report.WINDOWS:
            self.assertIn(f"ago({days}d)", query)
            self.assertIn(f"window_days={days}", query)
        self.assertIn("join kind=leftouter requestData on operation_Id", query)
        self.assertIn(retirement_report.ROUTING_MESSAGE, query)

    def test_parse_app_insights_response_maps_columns_and_ignores_extra_values(self) -> None:
        payload = {
            "tables": [{
                "columns": [{"name": "window_days"}, {"name": "api_mode"}],
                "rows": [[14, "v1", "ignored"]],
            }]
        }

        self.assertEqual(
            retirement_report.parse_app_insights_response(payload),
            [{"window_days": 14, "api_mode": "v1"}],
        )
        self.assertEqual(retirement_report.parse_app_insights_response({}), [])

    def test_mode_metrics_returns_rates_and_defaults(self) -> None:
        rows = [{
            "window_days": "14",
            "api_mode": "v1",
            "request_count": 10,
            "observed_request_count": 8,
            "failed_count": 2,
            "throttled_count": 1,
            "p50_ms": 10.0,
            "p95_ms": 20.0,
            "p99_ms": 30.0,
            "last_request": "2026-08-21T12:00:00Z",
        }]

        metrics = retirement_report.mode_metrics(rows, 14, "v1")

        self.assertEqual(metrics["success_rate"], 0.75)
        self.assertEqual(metrics["throttle_rate"], 0.125)
        self.assertIsNone(retirement_report.mode_metrics(rows, 7, "v1")["success_rate"])

    def test_parse_timestamp_normalizes_valid_values_and_rejects_invalid_values(self) -> None:
        parsed = retirement_report.parse_timestamp("2026-08-21T12:30:00Z")

        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(retirement_report.parse_timestamp("2026-08-21T12:30:00").tzinfo, timezone.utc)
        self.assertIsNone(retirement_report.parse_timestamp("not-a-date"))
        self.assertIsNone(retirement_report.parse_timestamp(None))

    @patch("retirement_report.subprocess.run")
    @patch("retirement_report.find_azure_cli", return_value="az.cmd")
    def test_query_azure_posts_query_and_parses_rows(self, _, run) -> None:
        run.return_value = SimpleNamespace(stdout=json.dumps({
            "tables": [{
                "columns": [{"name": "window_days"}, {"name": "api_mode"}],
                "rows": [[14, "v1"]],
            }]
        }))

        rows = retirement_report.query_azure("sub-1", "rg-1", "appi-1")

        self.assertEqual(rows, [{"window_days": 14, "api_mode": "v1"}])
        command = run.call_args.args[0]
        self.assertEqual(command[0:3], ["az.cmd", "rest", "--method"])
        self.assertIn(
            "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Insights/components/appi-1/query",
            command[command.index("--uri") + 1],
        )
        run.assert_called_once_with(command, check=True, capture_output=True, text=True)

    @patch("retirement_report.parse_args")
    def test_main_reads_bom_input_writes_report_and_requires_ready(self, parse_args) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "report.json"
            input_path.write_text("\ufeff" + json.dumps({"tables": []}), encoding="utf-8")
            parse_args.return_value = Namespace(
                input=input_path,
                application_insights_name=None,
                subscription_id=None,
                resource_group=None,
                legacy_p95_baseline_ms=None,
                min_success_rate=0.995,
                min_v1_requests=100,
                max_v1_last_request_age_hours=24,
                max_latency_increase=0.10,
                rollback_rehearsed=False,
                parity_passed=False,
                owner_approved=False,
                output=output_path,
                require_ready=True,
            )

            with patch("sys.stdout", new_callable=StringIO):
                exit_code = retirement_report.main()

            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ready"])

    @patch("retirement_report.parse_args")
    def test_main_rejects_incomplete_live_query_configuration(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            input=None,
            application_insights_name="appi-1",
            subscription_id=None,
            resource_group=None,
            legacy_p95_baseline_ms=None,
            min_success_rate=0.995,
            min_v1_requests=100,
            max_v1_last_request_age_hours=24,
            max_latency_increase=0.10,
            rollback_rehearsed=False,
            parity_passed=False,
            owner_approved=False,
            output=None,
            require_ready=False,
        )

        with patch("sys.stderr", new_callable=StringIO) as error:
            exit_code = retirement_report.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(error.getvalue())["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()