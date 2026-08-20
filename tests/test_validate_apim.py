import unittest
from unittest.mock import MagicMock, patch

from validate_apim import find_azure_cli, get_policy, is_resource_not_found, validate_snapshot


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
        return """<policies><inbound><choose>
    <trace source='openai-v1-migration'><metadata name='api_mode' /><metadata name='request_id' /></trace>
    <when condition='string.IsNullOrEmpty(X-API-Mode) or X-API-Mode v1'>
            <set-backend-service base-url='https://example.openai.azure.com' />
            <rewrite-uri template='/openai/v1/chat/completions' />
            <authentication-managed-identity resource='https://ai.azure.com' />
        </when>
        <when condition='X-API-Mode legacy'>
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


if __name__ == "__main__":
    unittest.main()