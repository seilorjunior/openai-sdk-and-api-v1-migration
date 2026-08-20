import unittest
from argparse import Namespace
import os
from unittest.mock import patch

import compare_responses
import load_test


class LoadAndCompareTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(load_test.percentile([10, 20, 30, 40], 0.95), 40)

    @patch("load_test.run_load")
    @patch("load_test.validate_configuration")
    @patch("load_test.parse_args")
    def test_both_modes_run_separately_and_pass_only_when_both_succeed(
        self,
        parse_args,
        validate_configuration,
        run_load,
    ) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="both",
            requests=4,
            concurrency=2,
            prompt="ready",
            confirm_large_load=False,
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

    @patch("load_test.parse_args")
    def test_large_load_requires_explicit_confirmation(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="apim",
            api_mode="both",
            requests=5_000,
            concurrency=1,
            prompt="ready",
            confirm_large_load=False,
        )

        with self.assertRaisesRegex(SystemExit, "require --confirm-large-load"):
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


if __name__ == "__main__":
    unittest.main()