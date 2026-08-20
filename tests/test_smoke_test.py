import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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


if __name__ == "__main__":
    unittest.main()