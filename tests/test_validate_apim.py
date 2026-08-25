import unittest
from unittest.mock import MagicMock, patch

from validate_apim import (
    REDACTED,
    find_azure_cli,
    get_policy,
    is_resource_not_found,
    redact_text,
    sanitize_snapshot,
    validate_snapshot,
)


def snapshot(api_path, backend_url, operation_path, policy):
    return {
        "api": {"properties": {"path": api_path, "serviceUrl": ""}},
        "apiPolicy": {"properties": {"value": policy}},
        "operations": [
            {
                "operation": {
                    "name": "chat-completions",
                    "properties": {"urlTemplate": operation_path},
                },
                "policy": {"properties": {"value": ""}},
            }
        ],
        "backends": [
            {
                "name": "model-backend",
                "properties": {"url": backend_url, "credentials": {}},
            }
        ],
        "namedValues": [],
    }


def unified_policy():
        return """<policies><inbound>
    <set-variable name='migration-api-mode' value='X-API-Mode' />
    <choose>
    <trace source='openai-v1-migration'><metadata name='api_mode' /><metadata name='request_id' /></trace>
    <when condition='context.Variables[ migration-api-mode default-v1 v1'>
            <set-backend-service base-url='https://example.openai.azure.com' />
            <rewrite-uri template='/openai/v1/chat/completions' />
            <authentication-managed-identity resource='https://ai.azure.com' />
        </when>
        <when condition='context.Variables[ migration-api-mode legacy'>
            <set-backend-service base-url='https://example.openai.azure.com' />
            <rewrite-uri template='/openai/deployments/example/chat/completions?api-version=2024-10-21' />
            <authentication-managed-identity resource='https://cognitiveservices.azure.com' />
        </when>
        <otherwise><return-response><set-body>invalid_api_mode</set-body></return-response></otherwise>
        </choose></inbound></policies>"""


class ValidateApimTests(unittest.TestCase):
    @patch("validate_apim.shutil.which")
    def test_finds_windows_azure_cli_command(self, which):
        which.side_effect = lambda command: "C:\\AzureCLI\\az.cmd" if command == "az.cmd" else None

        self.assertEqual("C:\\AzureCLI\\az.cmd", find_azure_cli())

    def test_recognizes_azure_cli_resource_not_found_response(self):
        error = RuntimeError('ERROR: Not Found({"error":{"code":"ResourceNotFound"}})')

        self.assertTrue(is_resource_not_found(error))

    @patch("validate_apim.urllib.request.urlopen")
    @patch("validate_apim.run_az_json")
    def test_policy_reader_accepts_utf8_bom(self, run_az_json, urlopen):
        run_az_json.return_value = {"accessToken": "token"}
        response = MagicMock()
        response.read.return_value = b'\xef\xbb\xbf{"properties":{"value":"<policies />"}}'
        urlopen.return_value.__enter__.return_value = response

        policy = get_policy("/subscriptions/example/policies/policy")

        self.assertEqual("<policies />", policy["properties"]["value"])

    def test_openai_v1_with_managed_identity_passes(self):
        current = snapshot(
            "openai/v1",
            "https://example.openai.azure.com/openai/v1",
            "/chat/completions",
            '<policies><inbound><authentication-managed-identity resource="https://ai.azure.com" /></inbound></policies>',
        )

        findings = validate_snapshot(current)

        self.assertEqual(["PASS"], [finding.severity for finding in findings])

    def test_legacy_models_route_and_api_version_fail(self):
        legacy = snapshot(
            "models",
            "https://example.services.ai.azure.com/models",
            "/chat/completions?api-version=2024-05-01-preview",
            '<policies><inbound><authentication-managed-identity resource="https://cognitiveservices.azure.com" /></inbound></policies>',
        )

        findings = validate_snapshot(legacy)
        checks = {finding.check for finding in findings if finding.severity == "ERROR"}

        self.assertIn("azure-ai-inference /models route", checks)
        self.assertIn("dated api-version", checks)
        self.assertIn("legacy Microsoft Entra scope", checks)
        self.assertIn("OpenAI v1 route", checks)

    def test_deployment_route_is_reported_as_legacy(self):
        legacy = snapshot(
            "openai",
            "https://example.openai.azure.com/openai",
            "/deployments/my-deployment/chat/completions",
            '<policies><inbound><rewrite-uri template="/openai/deployments/my-deployment/chat/completions?api-version=2024-10-21" /></inbound></policies>',
        )

        checks = {finding.check for finding in validate_snapshot(legacy) if finding.severity == "ERROR"}

        self.assertIn("deployment-based OpenAI route", checks)
        self.assertIn("dated api-version", checks)

    def test_existing_chat_operation_with_mode_selector_passes(self):
        current = snapshot(
            "openai",
            "https://example.openai.azure.com",
            "/v1/chat/completions",
            "",
        )
        current["operations"][0]["policy"]["properties"]["value"] = unified_policy()

        findings = validate_snapshot(current)

        self.assertEqual(["PASS"], [finding.severity for finding in findings])

    def test_incomplete_chat_mode_policy_fails(self):
        current = snapshot(
            "openai",
            "https://example.openai.azure.com",
            "/v1/chat/completions",
            "",
        )
        current["operations"][0]["policy"]["properties"]["value"] = (
            "<policies><inbound><choose><!-- X-API-Mode --></choose></inbound></policies>"
        )

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertIn("dual-mode chat operation", checks)

    def test_missing_v1_route_fails(self):
        current = snapshot(
            "openai",
            "https://example.openai.azure.com",
            "/chat/completions",
            '<policies><inbound><authentication-managed-identity resource="https://ai.azure.com" /></inbound></policies>',
        )

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertIn("OpenAI v1 route", checks)

    def test_missing_chat_operation_fails(self):
        current = snapshot(
            "openai/v1",
            "https://example.openai.azure.com/openai/v1",
            "/embeddings",
            '<policies><inbound><authentication-managed-identity resource="https://ai.azure.com" /></inbound></policies>',
        )

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertIn("chat completions operation", checks)

    def test_missing_backend_authentication_warns(self):
        current = snapshot(
            "openai/v1",
            "https://example.openai.azure.com/openai/v1",
            "/chat/completions",
            "<policies><inbound><base /></inbound></policies>",
        )

        warnings = {finding.check for finding in validate_snapshot(current) if finding.severity == "WARNING"}

        self.assertIn("backend authentication", warnings)

    def test_missing_invalid_api_mode_handling_fails(self):
        current = snapshot(
            "openai",
            "https://example.openai.azure.com",
            "/v1/chat/completions",
            "",
        )
        current["operations"][0]["policy"]["properties"]["value"] = unified_policy().replace(
            "<otherwise><return-response><set-body>invalid_api_mode</set-body></return-response></otherwise>",
            "<otherwise></otherwise>",
        )

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertIn("dual-mode chat operation", checks)

    def test_legacy_route_outside_dual_mode_policy_is_reported(self):
        current = snapshot(
            "openai",
            "https://example.openai.azure.com",
            "/legacy/chat/completions",
            '<policies><inbound><rewrite-uri template="/openai/deployments/example/chat/completions?api-version=2024-10-21" />'
            '<authentication-managed-identity resource="https://cognitiveservices.azure.com" /></inbound></policies>',
        )

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertIn("deployment-based OpenAI route", checks)
        self.assertIn("dated api-version", checks)
        self.assertIn("legacy Microsoft Entra scope", checks)


class RedactionTests(unittest.TestCase):
    def test_redact_text_hides_bearer_authorization_header(self):
        fake_token = "sometoken123456789ABCDEF"
        text = "Authorization" + ": Bearer " + fake_token

        redacted = redact_text(text)

        self.assertNotIn(fake_token, redacted)
        self.assertIn(REDACTED, redacted)

    def test_redact_text_hides_subscription_key(self):
        text = "Ocp-Apim-Subscription-Key: fakekey1234567890abcdef"

        redacted = redact_text(text)

        self.assertNotIn("fakekey1234567890abcdef", redacted)
        self.assertIn(REDACTED, redacted)

    def test_redact_text_hides_connection_string_secrets(self):
        text = (
            "Endpoint=sb://example.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;"
            "SharedAccessKey=fakeSharedAccessKeyValue123=;AccountKey=fakeAccountKeyValue456=="
        )

        redacted = redact_text(text)

        self.assertNotIn("fakeSharedAccessKeyValue123", redacted)
        self.assertNotIn("fakeAccountKeyValue456", redacted)

    def test_redact_text_hides_sas_signature_query_parameter(self):
        text = "https://example.blob.core.windows.net/container/blob?sv=2024-01-01&sig=fakeSignatureValue1234"

        redacted = redact_text(text)

        self.assertNotIn("fakeSignatureValue1234", redacted)

    def test_sanitize_snapshot_redacts_backend_credentials_and_named_value_secrets(self):
        current = snapshot(
            "openai/v1",
            "https://example.openai.azure.com/openai/v1",
            "/chat/completions",
            "<policies><inbound><authentication-managed-identity resource='https://ai.azure.com' /></inbound></policies>",
        )
        current["backends"][0]["properties"]["credentials"] = {
            "authorization": {"scheme": "Bearer", "parameter": "fake-backend-token-abc123"},
            "header": {"Ocp-Apim-Subscription-Key": ["fake-subscription-key-xyz789"]},
            "query": {"api-key": ["fake-query-secret-000111"]},
        }
        current["namedValues"] = [
            {
                "name": "backend-connection-string",
                "properties": {
                    "displayName": "backend-connection-string",
                    "secret": True,
                    "value": "Endpoint=sb://example/;SharedAccessKey=fake-named-value-secret",
                },
            }
        ]

        sanitized = sanitize_snapshot(current)
        rendered = str(sanitized)

        for secret in (
            "fake-backend-token-abc123",
            "fake-subscription-key-xyz789",
            "fake-query-secret-000111",
            "fake-named-value-secret",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)
        # Non-secret structure/content must be preserved.
        self.assertEqual("openai/v1", sanitized["api"]["properties"]["path"])

    def test_sanitize_snapshot_preserves_non_secret_policy_content(self):
        current = snapshot(
            "openai/v1",
            "https://example.openai.azure.com/openai/v1",
            "/v1/chat/completions",
            "",
        )
        current["operations"][0]["policy"]["properties"]["value"] = unified_policy()

        sanitized = sanitize_snapshot(current)

        self.assertEqual(
            unified_policy(),
            sanitized["operations"][0]["policy"]["properties"]["value"],
        )

    def test_legacy_configuration_finding_message_is_sanitized(self):
        fake_token = "leaked-fake-secret-value-123456"
        policy = (
            '<policies><inbound><authentication-managed-identity resource="https://cognitiveservices.azure.com" />'
            '<set-header name="Authorization" exists-action="override"><value>'
            + "Bearer " + fake_token +
            "</value></set-header></inbound></policies>"
        )
        current = snapshot(
            "models",
            "https://example.services.ai.azure.com/models",
            "/chat/completions?api-version=2024-05-01-preview",
            policy,
        )

        findings = validate_snapshot(current)

        for finding in findings:
            self.assertNotIn(fake_token, finding.message)


if __name__ == "__main__":
    unittest.main()