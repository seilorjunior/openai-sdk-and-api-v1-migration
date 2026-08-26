import unittest
from unittest.mock import patch

from validate_apim import find_azure_cli, validate_snapshot


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


class ValidateApimTests(unittest.TestCase):
    @patch("validate_apim.shutil.which")
    def test_finds_windows_azure_cli_command(self, which):
        which.side_effect = lambda command: "C:\\AzureCLI\\az.cmd" if command == "az.cmd" else None

        self.assertEqual("C:\\AzureCLI\\az.cmd", find_azure_cli())

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

    def test_documented_mcp_contract_with_expected_tools_passes(self):
        current = {
            "api": {
                "properties": {
                    "apiType": "mcp",
                    "type": "mcp",
                    "mcpProperties": {
                        "transportType": "streamable",
                        "endpoints": [{"name": "message", "uriTemplate": "/mcp"}],
                    },
                }
            },
            "runtimeMcpTools": [
                "list_migration_rules",
                "scan_migration_sources",
                "evaluate_retirement_readiness",
            ],
        }

        findings = validate_snapshot(current)

        self.assertEqual(["PASS"], [finding.severity for finding in findings])

    def test_live_stamp_mcp_shape_and_empty_tools_are_actionable_errors(self):
        current = {
            "api": {
                "properties": {
                    "apiType": "mcp",
                    "type": "mcp",
                    "mcpProperties": {
                        "endpoints": {"message": {"name": "message", "uriTemplate": "/mcp"}},
                    },
                }
            },
            "runtimeMcpTools": [],
        }

        checks = {finding.check for finding in validate_snapshot(current) if finding.severity == "ERROR"}

        self.assertEqual(
            {"MCP transport contract", "MCP endpoint contract", "MCP tool discovery"},
            checks,
        )


if __name__ == "__main__":
    unittest.main()