import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import capability_test


class CapabilityTestTests(unittest.TestCase):
    def test_missing_optional_deployment_is_skipped(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(capability_test.CapabilitySkipped, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"):
                capability_test.embeddings(Mock(), "test")

    def test_mutating_batch_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(capability_test.CapabilitySkipped, "--execute-mutating"):
            capability_test.batch(Mock(), "", False)

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


if __name__ == "__main__":
    unittest.main()