import json
import os
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import BadRequestError

import user_security_context_test


class UserSecurityContextTestTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("user_security_context_test.get_azd_value")
    def test_configure_lab_environment_fills_all_direct_values(self, get_azd_value: Mock) -> None:
        get_azd_value.side_effect = {
            "AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1",
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
        }.get

        user_security_context_test.configure_lab_environment("direct")

        self.assertEqual(
            os.environ["AZURE_OPENAI_BASE_URL"],
            "https://example.openai.azure.com/openai/v1",
        )
        self.assertEqual(os.environ["AZURE_OPENAI_DEPLOYMENT"], "chat-deployment")
        for name, value in user_security_context_test.LAB_SECURITY_CONTEXT_DEFAULTS.items():
            self.assertEqual(os.environ[name], value)

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
            "OPENAI_SECURITY_APPLICATION_NAME": "openai-migration-lab",
            "OPENAI_SECURITY_END_USER_ID": "11111111-1111-1111-1111-111111111111",
            "OPENAI_SECURITY_END_USER_TENANT_ID": "22222222-2222-2222-2222-222222222222",
            "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
        },
        clear=True,
    )
    def test_run_test_sends_exact_user_security_context(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="accepted"))]
        )

        result = user_security_context_test.run_test(client, "test")

        client.chat.completions.create.assert_called_once_with(
            model="chat-deployment",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=80,
            temperature=0,
            extra_body={
                "user_security_context": {
                    "application_name": "openai-migration-lab",
                    "end_user_id": "11111111-1111-1111-1111-111111111111",
                    "source_ip": "192.0.2.10",
                    "end_user_tenant_id": "22222222-2222-2222-2222-222222222222",
                }
            },
        )
        self.assertEqual(result, {"output_nonempty": True, "security_context_submitted": True})

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
            "OPENAI_SECURITY_APPLICATION_NAME": "openai-migration-lab",
            "OPENAI_SECURITY_END_USER_ID": "11111111-1111-1111-1111-111111111111",
        },
        clear=True,
    )
    def test_run_test_requires_source_ip_before_sending(self) -> None:
        client = Mock()

        with self.assertRaisesRegex(ValueError, "OPENAI_SECURITY_SOURCE_IP"):
            user_security_context_test.run_test(client, "test")

        client.chat.completions.create.assert_not_called()

    @patch(
        "user_security_context_test.parse_args",
        return_value=Namespace(
            target="apim",
            prompt="test",
            print_full_exchange=False,
            acknowledge_sensitive_output=False,
            save_full_exchange=None,
        ),
    )
    @patch("user_security_context_test.build_client")
    def test_main_reports_success_without_context_values(self, build_client: Mock, _: Mock) -> None:
        build_client.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="accepted"))]
        )
        context = {
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
            "OPENAI_SECURITY_APPLICATION_NAME": "private-application",
            "OPENAI_SECURITY_END_USER_ID": "private-user-id",
            "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
        }

        with patch.dict(os.environ, context, clear=True), patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = user_security_context_test.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload,
            {
                "output_nonempty": True,
                "security_context_submitted": True,
                "status": "passed",
                "target": "apim",
                "customer_explanation": user_security_context_test.CUSTOMER_EXPLANATION,
            },
        )
        for value in context.values():
            self.assertNotIn(value, output.getvalue())

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1",
            "AZURE_OPENAI_DEPLOYMENT": "DeepSeek-V4-Flash",
            "OPENAI_SECURITY_APPLICATION_NAME": "openai-migration-lab",
            "OPENAI_SECURITY_END_USER_ID": "11111111-1111-1111-1111-111111111111",
            "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
        },
        clear=True,
    )
    @patch(
        "user_security_context_test.parse_args",
        return_value=Namespace(
            target="direct",
            prompt="test",
            print_full_exchange=False,
            acknowledge_sensitive_output=False,
            save_full_exchange=None,
        ),
    )
    @patch("user_security_context_test.build_client")
    def test_main_classifies_rejected_user_security_context_as_unsupported(
        self, build_client: Mock, _: Mock
    ) -> None:
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://example.openai.azure.com/openai/v1/chat/completions"),
        )
        build_client.return_value.chat.completions.create.side_effect = BadRequestError(
            "Unrecognized request argument supplied: user_security_context",
            response=response,
            body={
                "code": "unrecognized_request_argument",
                "message": "Unrecognized request argument supplied: user_security_context",
            },
        )

        with patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = user_security_context_test.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "error_code": "unrecognized_request_argument",
                "reason": "deployment_or_api_does_not_support_user_security_context",
                "status": "unsupported",
                "target": "direct",
                "customer_explanation": user_security_context_test.CUSTOMER_EXPLANATION,
            },
        )

    @patch(
        "user_security_context_test.parse_args",
        return_value=Namespace(
            target="direct",
            prompt="test",
            print_full_exchange=True,
            acknowledge_sensitive_output=False,
            save_full_exchange=None,
        ),
    )
    def test_main_requires_acknowledgement_for_full_exchange(self, _: Mock) -> None:
        with patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = user_security_context_test.main()

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertIn("--acknowledge-sensitive-output", payload["error"])

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1",
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
            "OPENAI_SECURITY_APPLICATION_NAME": "private-application",
            "OPENAI_SECURITY_END_USER_ID": "private-user-id",
            "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
        },
        clear=True,
    )
    @patch(
        "user_security_context_test.parse_args",
        return_value=Namespace(
            target="direct",
            prompt="test",
            print_full_exchange=True,
            acknowledge_sensitive_output=True,
            save_full_exchange=None,
        ),
    )
    @patch("user_security_context_test.build_client")
    def test_main_prints_complete_request_and_response_when_acknowledged(
        self, build_client: Mock, _: Mock
    ) -> None:
        response = Mock()
        response.choices = [SimpleNamespace(message=SimpleNamespace(content="accepted"))]
        response.model_dump.return_value = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"role": "assistant", "content": "accepted"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }
        build_client.return_value.chat.completions.create.return_value = response

        with patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = user_security_context_test.main()

        self.assertEqual(exit_code, 0)
        exchange = json.loads(output.getvalue())["full_exchange"]
        self.assertEqual(exchange["request_body"]["model"], "chat-deployment")
        self.assertEqual(
            exchange["request_body"]["user_security_context"]["end_user_id"],
            "private-user-id",
        )
        self.assertNotIn("extra_body", exchange["request_body"])
        self.assertEqual(exchange["response_body"]["id"], "chatcmpl-test")
        self.assertNotIn("authorization", output.getvalue().lower())
        self.assertNotIn("api_key", output.getvalue().lower())

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1",
            "AZURE_OPENAI_DEPLOYMENT": "chat-deployment",
            "OPENAI_SECURITY_APPLICATION_NAME": "private-application",
            "OPENAI_SECURITY_END_USER_ID": "private-user-id",
            "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
        },
        clear=True,
    )
    @patch("user_security_context_test.build_client")
    def test_main_saves_complete_request_and_response_when_acknowledged(self, build_client: Mock) -> None:
        response = Mock()
        response.choices = [SimpleNamespace(message=SimpleNamespace(content="accepted"))]
        response.model_dump.return_value = {
            "id": "chatcmpl-saved",
            "choices": [{"message": {"role": "assistant", "content": "accepted"}}],
        }
        build_client.return_value.chat.completions.create.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "user-security-context-exchange.json"
            args = Namespace(
                target="direct",
                prompt="test",
                print_full_exchange=False,
                acknowledge_sensitive_output=True,
                save_full_exchange=output_path,
            )
            with (
                patch("user_security_context_test.parse_args", return_value=args),
                patch("sys.stdout", new_callable=StringIO) as output,
            ):
                exit_code = user_security_context_test.main()

            exchange = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(exchange["request_body"]["user_security_context"]["end_user_id"], "private-user-id")
        self.assertEqual(exchange["response_body"]["id"], "chatcmpl-saved")
        self.assertNotIn("full_exchange", json.loads(output.getvalue()))
        self.assertNotIn("authorization", json.dumps(exchange).lower())
        self.assertNotIn("api_key", json.dumps(exchange).lower())


if __name__ == "__main__":
    unittest.main()