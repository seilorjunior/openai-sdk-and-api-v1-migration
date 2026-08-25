import asyncio
import os
import unittest
from argparse import Namespace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import smoke_test


class SmokeTestClientTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"LEGACY_MODELS_BASE_URL": "https://example.services.ai.azure.com/models"},
        clear=True,
    )
    @patch("smoke_test.ChatCompletionsClient")
    @patch("smoke_test.DefaultAzureCredential")
    def test_legacy_client_uses_inference_endpoint_and_scope(
        self,
        credential_type: Mock,
        legacy_client_type: Mock,
    ) -> None:
        credential = credential_type.return_value

        smoke_test.build_chat_client("legacy", "direct")

        legacy_client_type.assert_called_once_with(
            endpoint="https://example.services.ai.azure.com/models",
            credential=credential,
            credential_scopes=["https://cognitiveservices.azure.com/.default"],
        )

    @patch.dict(
        os.environ,
        {
            "APIM_OPENAI_BASE_URL": "https://example.azure-api.net/openai/v1/",
            "APIM_SUBSCRIPTION_KEY": "test-key",
        },
        clear=True,
    )
    @patch("smoke_test.OpenAI")
    def test_legacy_apim_client_uses_existing_v1_route_and_selector(self, openai_type: Mock) -> None:
        smoke_test.build_chat_client("legacy", "apim")

        openai_type.assert_called_once_with(
            api_key="apim-managed-backend-auth",
            base_url="https://example.azure-api.net/openai/v1/",
            default_headers={
                "Ocp-Apim-Subscription-Key": "test-key",
                "X-API-Mode": "legacy",
            },
            timeout=30.0,
            max_retries=2,
        )

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    @patch("smoke_test.build_chat_client")
    def test_legacy_apim_chat_uses_openai_contract(self, build_chat_client: Mock) -> None:
        client = build_chat_client.return_value
        client.chat.completions.create.return_value = SimpleNamespace(
            model="gpt-test",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ready", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1),
        )

        result = smoke_test.invoke_chat("legacy", "apim", "test", max_tokens=20)

        client.chat.completions.create.assert_called_once_with(
            model="chat-deployment",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=20,
            temperature=0,
        )
        self.assertEqual(result.content, "ready")

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    @patch("smoke_test.build_chat_client")
    def test_legacy_chat_uses_complete_and_returns_common_result(self, build_chat_client: Mock) -> None:
        client = build_chat_client.return_value
        client.complete.return_value = SimpleNamespace(
            model="gpt-test",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ready", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1),
        )

        result = smoke_test.invoke_chat("legacy", "direct", "test", max_tokens=20)

        client.complete.assert_called_once_with(
            model="chat-deployment",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=20,
            temperature=0,
        )
        self.assertEqual(result.content, "ready")
        self.assertEqual(result.input_tokens, 4)
        self.assertEqual(result.output_tokens, 1)

    @patch.dict(os.environ, {"AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1/"}, clear=True)
    @patch("smoke_test.OpenAI")
    @patch("smoke_test.get_bearer_token_provider")
    @patch("smoke_test.DefaultAzureCredential")
    def test_direct_client_uses_entra_v1_scope(
        self,
        credential_type: Mock,
        get_token_provider: Mock,
        openai_type: Mock,
    ) -> None:
        credential = credential_type.return_value
        token_provider = get_token_provider.return_value

        smoke_test.build_client("direct")

        get_token_provider.assert_called_once_with(credential, "https://ai.azure.com/.default")
        openai_type.assert_called_once_with(
            api_key=token_provider,
            base_url="https://example.openai.azure.com/openai/v1/",
            timeout=30.0,
            max_retries=2,
        )

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_BASE_URL": "https://example.cognitiveservices.azure.com/openai/v1/",
            "AZURE_OPENAI_TENANT_ID": "33333333-3333-3333-3333-333333333333",
        },
        clear=True,
    )
    @patch("smoke_test.OpenAI")
    @patch("smoke_test.get_bearer_token_provider")
    @patch("smoke_test.AzureCliCredential")
    def test_direct_client_can_select_azure_cli_tenant(
        self,
        credential_type: Mock,
        get_token_provider: Mock,
        _: Mock,
    ) -> None:
        smoke_test.build_client("direct")

        credential_type.assert_called_once_with(tenant_id="33333333-3333-3333-3333-333333333333")
        get_token_provider.assert_called_once_with(
            credential_type.return_value,
            "https://ai.azure.com/.default",
        )

    @patch.dict(
        os.environ,
        {
            "APIM_OPENAI_BASE_URL": "https://example.azure-api.net/openai/v1/",
            "APIM_SUBSCRIPTION_KEY": "test-key",
        },
        clear=True,
    )
    @patch("smoke_test.OpenAI")
    def test_apim_client_uses_subscription_key(self, openai_type: Mock) -> None:
        smoke_test.build_client("apim")

        openai_type.assert_called_once_with(
            api_key="apim-managed-backend-auth",
            base_url="https://example.azure-api.net/openai/v1/",
            default_headers={"Ocp-Apim-Subscription-Key": "test-key"},
            timeout=30.0,
            max_retries=2,
        )

    @patch.dict(
        os.environ,
        {
            "APIM_OPENAI_BASE_URL": "https://example.azure-api.net/openai/v1/",
            "APIM_SUBSCRIPTION_KEY": "test-key",
            "OPENAI_TIMEOUT_SECONDS": "5",
            "OPENAI_MAX_RETRIES": "4",
        },
        clear=True,
    )
    def test_retry_and_timeout_are_configurable(self) -> None:
        options = smoke_test.client_options("apim")

        self.assertEqual(options["timeout"], 5.0)
        self.assertEqual(options["max_retries"], 4)

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_send_chat_defaults_optional_response_fields(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            model=None,
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=None,
            )],
            usage=None,
        )

        result = smoke_test.send_chat(client, "v1", "direct", "fallback-model", "test")

        self.assertEqual(result.model, "fallback-model")
        self.assertEqual(result.content, "")
        self.assertEqual(result.input_tokens, 0)
        self.assertEqual(result.output_tokens, 0)
        self.assertEqual(result.tool_call_count, 0)

    @patch.dict(os.environ, {}, clear=True)
    def test_require_env_rejects_missing_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "AZURE_OPENAI_DEPLOYMENT"):
            smoke_test.require_env("AZURE_OPENAI_DEPLOYMENT")

    @patch("smoke_test.parse_args")
    @patch("smoke_test.invoke_chat")
    def test_main_returns_one_for_empty_response(self, invoke_chat, parse_args) -> None:
        parse_args.return_value = Namespace(target="direct", api_mode="v1", prompt="test", cancel_after=None)
        invoke_chat.return_value = smoke_test.ChatResult("model", "   ", "stop", 1, 1, 0)

        with patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = smoke_test.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("response_present=false", output.getvalue())

    @patch("smoke_test.parse_args")
    def test_main_rejects_legacy_cancellation(self, parse_args) -> None:
        parse_args.return_value = Namespace(target="direct", api_mode="legacy", prompt="test", cancel_after=0.1)

        with patch("sys.stderr", new_callable=StringIO) as error:
            exit_code = smoke_test.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("unavailable in legacy mode", error.getvalue())


class CancellationProbeTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    @patch("smoke_test.client_options", return_value={})
    @patch("smoke_test.AsyncOpenAI")
    async def test_cancellation_probe_cancels_pending_request_and_closes_client(self, async_openai, _) -> None:
        client = async_openai.return_value
        client.close = AsyncMock()

        async def pending_request(**_kwargs):
            await asyncio.Event().wait()

        client.responses.create.side_effect = pending_request

        cancelled = await smoke_test.cancellation_probe("direct", 0)

        self.assertTrue(cancelled)
        client.close.assert_awaited_once()

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    @patch("smoke_test.client_options", return_value={})
    @patch("smoke_test.AsyncOpenAI")
    async def test_cancellation_probe_reports_completed_request_and_closes_client(self, async_openai, _) -> None:
        client = async_openai.return_value
        client.close = AsyncMock()
        client.responses.create = AsyncMock(return_value=SimpleNamespace(status="completed"))

        cancelled = await smoke_test.cancellation_probe("direct", 0)

        self.assertFalse(cancelled)
        client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()