import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from services.migration_mcp.app import (
    MAX_SOURCE_BYTES,
    SourceDocument,
    allowed_mcp_hosts,
    app,
    list_rules,
    scan_sources,
)


class MigrationMCPTests(unittest.TestCase):
    def test_list_rules_exposes_stable_rule_contract(self) -> None:
        rules = list_rules()

        self.assertEqual(rules[0]["rule_id"], "AOAI001")
        self.assertEqual(rules[-1]["rule_id"], "AOAI006")

    def test_scan_sources_returns_structured_findings(self) -> None:
        report = scan_sources([
            SourceDocument(path="src/client.py", source="client = AzureOpenAI()\n"),
            SourceDocument(path="requirements.txt", source="azure-ai-inference>=1.0\n"),
        ])

        self.assertEqual(report["source_count"], 2)
        self.assertEqual(report["finding_count"], 2)
        self.assertEqual(report["counts_by_rule"], {"AOAI005": 1, "AOAI001": 1})

    def test_scan_sources_rejects_oversized_and_duplicate_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            scan_sources([SourceDocument(path="large.py", source="x" * (MAX_SOURCE_BYTES + 1))])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            scan_sources([
                SourceDocument(path="same.py", source="first"),
                SourceDocument(path="same.py", source="second"),
            ])

    def test_health_is_public_and_mcp_requires_backend_key(self) -> None:
        with TestClient(app) as client, patch.dict(os.environ, {"MCP_BACKEND_KEY": "expected"}):
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(client.post("/mcp").status_code, 401)
            authorized = client.post("/mcp", headers={"x-mcp-backend-key": "expected"})
            unknown_host = client.post(
                "/mcp",
                headers={"host": "unknown.example.com", "x-mcp-backend-key": "expected"},
                json={},
            )

        self.assertNotEqual(authorized.status_code, 401)
        self.assertEqual(unknown_host.status_code, 421)

    def test_mcp_host_allowlist_uses_azure_hostname_and_rejects_unknown_hosts(self) -> None:
        with patch.dict(
            os.environ,
            {"WEBSITE_HOSTNAME": "app.azurewebsites.net", "MCP_ALLOWED_HOSTS": "custom.example.com"},
        ):
            self.assertIn("app.azurewebsites.net", allowed_mcp_hosts())
            self.assertIn("custom.example.com", allowed_mcp_hosts())


if __name__ == "__main__":
    unittest.main()