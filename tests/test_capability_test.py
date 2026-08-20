import json
import os
import unittest
from argparse import Namespace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import capability_test


class CapabilityTestTests(unittest.TestCase):
    def test_missing_optional_deployment_is_skipped(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(capability_test.CapabilitySkipped, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"):
                capability_test.embeddings(Mock(), "test")

    def test_mutating_batch_requires_explicit_confirmation(self) -> None:
        client = Mock()

        with self.assertRaisesRegex(capability_test.CapabilitySkipped, "--execute-mutating"):
            capability_test.batch(client, "", False)

        client.files.create.assert_not_called()
        client.batches.create.assert_not_called()

    def test_run_capability_dispatches_mutating_operation_with_confirmation(self) -> None:
        client = Mock()
        with patch("capability_test.batch", return_value={"batch_id": "batch-1"}) as batch:
            result = capability_test.run_capability(client, "batch", "prompt", True)

        batch.assert_called_once_with(client, "prompt", True)
        self.assertEqual(result["batch_id"], "batch-1")
        self.assertIn("latency_ms", result)

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_structured_output_validates_expected_shape(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ready": true, "summary": "ready"}'))]
        )

        result = capability_test.structured(client, "test")

        self.assertTrue(result["schema_valid"])
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_tool_call_arguments_are_validated(self) -> None:
        tool_call = SimpleNamespace(function=SimpleNamespace(name="get_migration_status", arguments='{"application":"demo"}'))
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
        )

        result = capability_test.tools(client, "test")

        self.assertEqual(result["tool_name"], "get_migration_status")
        self.assertTrue(result["arguments_valid_json"])

    @patch("capability_test.parse_args")
    def test_legacy_mode_fails_before_building_client(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="legacy",
            capability="chat",
            prompt="test",
            execute_mutating=False,
        )

        with patch("capability_test.build_client") as build_client, patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = capability_test.main()

        self.assertEqual(exit_code, 1)
        build_client.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "failed", "error": "Advanced capabilities require --api-mode v1."},
        )

    @patch("capability_test.parse_args")
    @patch("capability_test.build_client", return_value=Mock())
    def test_skipped_capability_does_not_fail_process(self, _, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="v1",
            capability="embeddings",
            prompt="test",
            execute_mutating=False,
        )

        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = capability_test.main()

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", result["reason"])

    @patch("capability_test.parse_args")
    @patch("capability_test.build_client", return_value=Mock())
    def test_failure_output_does_not_expose_exception_message(self, _, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="v1",
            capability="chat",
            prompt="test",
            execute_mutating=False,
        )

        with patch("capability_test.run_capability", side_effect=ValueError("secret endpoint detail")), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            exit_code = capability_test.main()

        rendered = output.getvalue()
        result = json.loads(rendered)
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error_type"], "ValueError")
        self.assertNotIn("secret endpoint detail", rendered)


if __name__ == "__main__":
    unittest.main()