import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import httpx

import compare_responses
import load_test
import retirement_report


class LoadAndCompareTests(unittest.TestCase):
    def test_retirement_report_fails_closed_without_telemetry_or_approvals(self) -> None:
        report = retirement_report.evaluate([], legacy_p95_baseline_ms=None)

        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["telemetry_complete"])
        self.assertFalse(report["checks"]["v1_success_rate"])
        self.assertFalse(report["checks"]["v1_p95_within_approved_baseline"])

    def test_retirement_report_passes_with_clean_14_day_evidence_and_approvals(self) -> None:
        rows = [
            {
                "window_days": 14,
                "api_mode": "v1",
                "request_count": 1000,
                "observed_request_count": 1000,
                "failed_count": 1,
                "throttled_count": 2,
                "p50_ms": 80.0,
                "p95_ms": 105.0,
                "p99_ms": 150.0,
                "last_request": "2026-07-01T00:00:00Z",
            }
        ]

        report = retirement_report.evaluate(
            rows,
            legacy_p95_baseline_ms=100.0,
            rollback_rehearsed=True,
            parity_passed=True,
            owner_approved=True,
        )

        self.assertTrue(report["ready"])
        self.assertEqual(report["windows"]["14"]["combined_v1"]["success_rate"], 0.999)
        self.assertEqual(report["thresholds"]["observed_v1_p95_ms"], 105.0)

    def test_retirement_report_requires_complete_correlated_request_telemetry(self) -> None:
        rows = [
            {
                "window_days": 14,
                "api_mode": "default-v1",
                "request_count": 10,
                "observed_request_count": 9,
                "failed_count": 0,
                "throttled_count": 0,
                "p95_ms": 50.0,
            }
        ]

        report = retirement_report.evaluate(
            rows,
            legacy_p95_baseline_ms=50.0,
            rollback_rehearsed=True,
            parity_passed=True,
            owner_approved=True,
        )

        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["telemetry_complete"])

    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(load_test.percentile([10, 20, 30, 40], 0.95), 40)

    def test_classify_failure_detects_transport_errors(self) -> None:
        from openai import APIConnectionError

        request = httpx.Request("POST", "https://example.test/openai/v1/chat/completions")
        error = APIConnectionError(request=request)

        self.assertEqual(load_test.classify_failure(error), "transport")

    def test_classify_failure_detects_request_errors(self) -> None:
        self.assertEqual(load_test.classify_failure(ValueError("bad config")), "request")

    def test_classify_failure_defaults_to_other(self) -> None:
        self.assertEqual(load_test.classify_failure(RuntimeError("unexpected")), "other")

    def test_estimate_cost_is_none_without_configured_rates(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_test.estimate_cost(1000, 1000))

    def test_estimate_cost_uses_configured_rates(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENAI_INPUT_USD_PER_1M_TOKENS": "1", "OPENAI_OUTPUT_USD_PER_1M_TOKENS": "2"},
            clear=True,
        ):
            cost = load_test.estimate_cost(1_000_000, 500_000)

        self.assertEqual(cost, 2.0)

    @patch("load_test.build_chat_client")
    def test_thread_client_is_built_once_and_reused_per_mode(self, build_chat_client) -> None:
        load_test._thread_local.clients = {}
        build_chat_client.return_value = object()

        first = load_test.get_thread_client("apim", "v1")
        second = load_test.get_thread_client("apim", "v1")

        self.assertIs(first, second)
        build_chat_client.assert_called_once_with("v1", "apim")

    @patch("load_test.build_chat_client")
    def test_thread_client_is_cached_separately_per_api_mode(self, build_chat_client) -> None:
        load_test._thread_local.clients = {}
        build_chat_client.side_effect = [object(), object()]

        v1_client = load_test.get_thread_client("apim", "v1")
        legacy_client = load_test.get_thread_client("apim", "legacy")

        self.assertIsNot(v1_client, legacy_client)
        self.assertEqual(build_chat_client.call_count, 2)

    @patch("load_test.invoke")
    def test_run_load_separates_transport_and_request_failure_categories(self, invoke) -> None:
        from openai import APIConnectionError

        request = httpx.Request("POST", "https://example.test/openai/v1/chat/completions")
        invoke.side_effect = [
            APIConnectionError(request=request),
            ValueError("Set the AZURE_OPENAI_DEPLOYMENT environment variable."),
            {"latency_ms": 10.0, "input_tokens": 1, "output_tokens": 1},
        ]

        report = load_test.run_load("direct", "v1", 3, 1, "ready")

        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["failed"], 2)
        self.assertEqual(report["failures_by_category"]["transport"], 1)
        self.assertEqual(report["failures_by_category"]["request"], 1)

    @patch("load_test.invoke")
    def test_warmup_raises_request_failure(self, invoke) -> None:
        invoke.side_effect = ValueError("invalid configuration")

        with self.assertRaisesRegex(ValueError, "invalid configuration"):
            load_test.run_warmup("direct", "v1", 1, 1, "ready")

    @patch("load_test.run_warmup")
    @patch("load_test.run_load")
    @patch("load_test.validate_configuration")
    @patch("load_test.parse_args")
    def test_both_modes_run_separately_and_pass_only_when_both_succeed(
        self,
        parse_args,
        validate_configuration,
        run_load,
        run_warmup,
    ) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="both",
            requests=4,
            concurrency=2,
            prompt="ready",
            confirm_large_load=False,
            warmup_requests=0,
        )
        run_load.side_effect = [
            {"api_mode": "v1", "failed": 0},
            {"api_mode": "legacy", "failed": 0},
        ]

        exit_code = load_test.main()

        self.assertEqual(exit_code, 0)
        validate_configuration.assert_called_once_with("apim", ("v1", "legacy"))
        self.assertEqual(
            run_load.call_args_list,
            [
                unittest.mock.call("apim", "v1", 4, 2, "ready"),
                unittest.mock.call("apim", "legacy", 4, 2, "ready"),
            ],
        )

    @patch("load_test.run_warmup")
    @patch("load_test.run_load")
    @patch("load_test.validate_configuration")
    @patch("load_test.parse_args")
    def test_warmup_failure_aborts_before_measured_load(
        self,
        parse_args,
        validate_configuration,
        run_load,
        run_warmup,
    ) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="v1",
            requests=4,
            concurrency=2,
            prompt="ready",
            confirm_large_load=False,
            warmup_requests=1,
        )
        run_warmup.side_effect = RuntimeError("sensitive backend response")

        with patch("sys.stderr") as stderr:
            exit_code = load_test.main()

        self.assertEqual(exit_code, 1)
        run_load.assert_not_called()
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('"phase": "warmup"', output)
        self.assertIn('"error_type": "RuntimeError"', output)
        self.assertNotIn("sensitive backend response", output)

    @patch("load_test.parse_args")
    def test_large_load_requires_explicit_confirmation(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="both",
            requests=5_000,
            concurrency=1,
            prompt="ready",
            confirm_large_load=False,
            warmup_requests=0,
        )

        with self.assertRaisesRegex(SystemExit, "require --confirm-large-load"):
            load_test.main()

    @patch("load_test.parse_args")
    def test_warmup_requests_out_of_range_is_rejected(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="v1",
            requests=4,
            concurrency=1,
            prompt="ready",
            confirm_large_load=False,
            warmup_requests=load_test.MAX_WARMUP_REQUESTS + 1,
        )

        with self.assertRaisesRegex(SystemExit, "--warmup-requests must be between"):
            load_test.main()

    @patch.dict(os.environ, {}, clear=True)
    def test_apim_configuration_reports_the_first_missing_variable(self) -> None:
        with self.assertRaisesRegex(ValueError, "AZURE_OPENAI_DEPLOYMENT"):
            load_test.validate_configuration("apim", ("v1", "legacy"))

    def test_behavior_comparison_ignores_exact_generated_text(self) -> None:
        legacy = {
            "output_nonempty": True,
            "finish_reason": "stop",
            "tool_call_count": 0,
            "output_length": 10,
        }
        current = {**legacy, "output_length": 15}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertTrue(report["passed"])
        self.assertEqual(report["output_length_ratio"], 1.5)

    def test_compare_handles_zero_length_output_on_both_sides_without_division_error(self) -> None:
        legacy = {
            "output_nonempty": False,
            "finish_reason": "content_filter",
            "tool_call_count": 0,
            "output_length": 0,
        }
        current = {**legacy}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertTrue(report["passed"])
        self.assertEqual(report["output_length_ratio"], 0.0)

    def test_compare_handles_zero_length_output_on_one_side_without_division_error(self) -> None:
        legacy = {
            "output_nonempty": True,
            "finish_reason": "stop",
            "tool_call_count": 0,
            "output_length": 0,
        }
        current = {**legacy, "output_nonempty": False, "output_length": 12}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["nonempty_match"])
        self.assertEqual(report["output_length_ratio"], 12.0)

    def test_compare_fails_on_finish_reason_mismatch(self) -> None:
        legacy = {
            "output_nonempty": True,
            "finish_reason": "stop",
            "tool_call_count": 0,
            "output_length": 10,
        }
        current = {**legacy, "finish_reason": "length"}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["finish_reason_match"])

    def test_compare_fails_on_tool_call_count_mismatch(self) -> None:
        legacy = {
            "output_nonempty": True,
            "finish_reason": "tool_calls",
            "tool_call_count": 1,
            "output_length": 10,
        }
        current = {**legacy, "tool_call_count": 0}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["tool_call_count_match"])

    def test_compare_fails_when_length_ratio_exceeds_limit(self) -> None:
        legacy = {
            "output_nonempty": True,
            "finish_reason": "stop",
            "tool_call_count": 0,
            "output_length": 10,
        }
        current = {**legacy, "output_length": 100}

        report = compare_responses.compare(legacy, current, 2.0)

        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["length_ratio_within_limit"])
        self.assertEqual(report["output_length_ratio"], 10.0)

    def test_normalize_maps_chat_result_fields(self) -> None:
        from smoke_test import ChatResult

        result = ChatResult(
            model="test-model",
            content=" ready ",
            finish_reason="stop",
            input_tokens=3,
            output_tokens=1,
            tool_call_count=0,
        )

        normalized = compare_responses.normalize(result)

        self.assertEqual(
            normalized,
            {
                "output_nonempty": True,
                "finish_reason": "stop",
                "tool_call_count": 0,
                "output_length": len(" ready "),
                "input_tokens": 3,
                "output_tokens": 1,
            },
        )

    def test_normalize_reports_empty_output(self) -> None:
        from smoke_test import ChatResult

        result = ChatResult(
            model="test-model",
            content="   ",
            finish_reason="content_filter",
            input_tokens=0,
            output_tokens=0,
            tool_call_count=0,
        )

        normalized = compare_responses.normalize(result)

        self.assertFalse(normalized["output_nonempty"])
        self.assertEqual(normalized["output_length"], 3)

    def test_load_corpus_rejects_duplicate_scenario_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "corpus.jsonl"
            corpus.write_text(
                '{"id":"same","prompt":"first"}\n{"id":"same","prompt":"second"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Duplicate corpus scenario id"):
                compare_responses.load_corpus(corpus)

    @patch("compare_responses.v1_chat")
    @patch("compare_responses.legacy_chat")
    def test_compare_corpus_reports_each_scenario_and_enforces_pass_rate(self, legacy_chat, v1_chat) -> None:
        from smoke_test import ChatResult

        legacy_chat.side_effect = [
            ChatResult("model", "ready", "stop", 1, 1, 0),
            ChatResult("model", "", "tool_calls", 2, 1, 1),
        ]
        v1_chat.side_effect = [
            ChatResult("model", "ready", "stop", 1, 1, 0),
            ChatResult("model", "not-a-tool", "stop", 2, 1, 0),
        ]
        scenarios = [
            {"id": "deterministic", "prompt": "ready"},
            {
                "id": "tool-call",
                "prompt": "Call lookup.",
                "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                "tool_choice": "required",
                "expected_finish_reason": "tool_calls",
                "expected_tool_call_count": 1,
            },
        ]

        report = compare_responses.compare_corpus("direct", scenarios, 2.0, 1.0)

        self.assertFalse(report["passed"])
        self.assertEqual(report["pass_rate"], 0.5)
        self.assertEqual([result["id"] for result in report["scenarios"]], ["deterministic", "tool-call"])
        legacy_chat.assert_any_call(
            "direct",
            "Call lookup.",
            80,
            {"tools": scenarios[1]["tools"], "tool_choice": "required"},
        )

    @patch("compare_responses.v1_chat")
    @patch("compare_responses.legacy_chat")
    def test_compare_corpus_allows_configured_aggregate_threshold(self, legacy_chat, v1_chat) -> None:
        from smoke_test import ChatResult

        matching = ChatResult("model", "ready", "stop", 1, 1, 0)
        mismatch = ChatResult("model", "ready", "length", 1, 1, 0)
        legacy_chat.side_effect = [matching, matching]
        v1_chat.side_effect = [matching, mismatch]

        report = compare_responses.compare_corpus(
            "apim",
            [{"id": "one", "prompt": "one"}, {"id": "two", "prompt": "two"}],
            2.0,
            0.5,
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["passed_count"], 1)

    @patch("compare_responses.v1_chat")
    @patch("compare_responses.legacy_chat")
    def test_main_reports_error_type_and_nonzero_exit_on_sdk_failure(self, legacy_chat, v1_chat) -> None:
        legacy_chat.side_effect = ValueError("Set the AZURE_OPENAI_BASE_URL environment variable.")

        with patch(
            "compare_responses.parse_args",
            return_value=Namespace(target="direct", prompt="ready", max_length_ratio=2.0),
        ), patch("builtins.print") as mock_print:
            exit_code = compare_responses.main()

        self.assertEqual(exit_code, 1)
        printed = mock_print.call_args[0][0]
        self.assertIn("ValueError", printed)
        self.assertNotIn("AZURE_OPENAI_BASE_URL", printed)


if __name__ == "__main__":
    unittest.main()