#!/usr/bin/env python3
"""Validate APIM configuration for an Azure OpenAI /openai/v1 migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

LEGACY_PATTERNS = {
    "azure-ai-inference /models route": re.compile(r"(?:services\.ai\.azure\.com)?/models(?:/|\b)", re.I),
    "dated api-version": re.compile(r"api-version(?:=|&amp;|%3[dD])", re.I),
    "deployment-based OpenAI route": re.compile(r"/openai/deployments/", re.I),
    "legacy Microsoft Entra scope": re.compile(r"https://cognitiveservices\.azure\.com(?:/\.default)?", re.I),
}

EXPECTED_MCP_TOOLS = {
    "evaluate_retirement_readiness",
    "list_migration_rules",
    "scan_migration_sources",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str


def find_azure_cli() -> str:
    executable = shutil.which("az") or shutil.which("az.cmd")
    if not executable:
        raise RuntimeError("Azure CLI executable was not found on PATH.")
    return executable


def run_az_json(arguments: list[str]) -> Any:
    command = [find_azure_cli(), *arguments, "--output", "json", "--only-show-errors"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Azure CLI failed: {' '.join(command)}")
    return json.loads(result.stdout)


def get_resource(resource_id: str, api_version: str = "2024-05-01") -> dict[str, Any]:
    return run_az_json(["rest", "--method", "get", "--url", f"{resource_id}?api-version={api_version}"])


def get_apim_snapshot(resource_group: str, service_name: str, api_id: str) -> dict[str, Any]:
    subscription_id = run_az_json(["account", "show", "--query", "id"])
    service_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{service_name}"
    )
    api_resource_id = f"{service_id}/apis/{api_id}"
    api = get_resource(api_resource_id, "2025-09-01-preview")
    try:
        api_policy = get_resource(f"{api_resource_id}/policies/policy", "2024-05-01")
    except RuntimeError as error:
        if "404" not in str(error):
            raise
        api_policy = {"properties": {"value": ""}}
    operations = run_az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{api_resource_id}/operations?api-version=2024-05-01",
            "--query",
            "value",
        ]
    )
    operation_details = []
    for operation in operations:
        operation_id = operation["name"]
        policy_id = f"{api_resource_id}/operations/{operation_id}/policies/policy"
        try:
            policy = get_resource(policy_id)
        except RuntimeError as error:
            if "404" not in str(error):
                raise
            policy = {"properties": {"value": ""}}
        operation_details.append({"operation": operation, "policy": policy})

    backends = run_az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{service_id}/backends?api-version=2024-05-01",
            "--query",
            "value",
        ]
    )
    named_values = run_az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{service_id}/namedValues?api-version=2024-05-01",
            "--query",
            "value[].{{name:name,properties:{{displayName:properties.displayName,secret:properties.secret}}}}",
        ]
    )
    return {
        "api": api,
        "apiPolicy": api_policy,
        "operations": operation_details,
        "backends": backends,
        "namedValues": named_values,
    }


def iter_text(snapshot: dict[str, Any]) -> Iterable[tuple[str, str]]:
    api_properties = snapshot.get("api", {}).get("properties", {})
    yield "API path", str(api_properties.get("path", ""))
    yield "API service URL", str(api_properties.get("serviceUrl", ""))
    yield "API policy", str(snapshot.get("apiPolicy", {}).get("properties", {}).get("value", ""))

    for item in snapshot.get("operations", []):
        operation = item.get("operation", {})
        operation_id = operation.get("name", "unknown")
        properties = operation.get("properties", {})
        yield f"Operation {operation_id} URL template", str(properties.get("urlTemplate", ""))
        yield f"Operation {operation_id} policy", str(item.get("policy", {}).get("properties", {}).get("value", ""))

    for backend in snapshot.get("backends", []):
        backend_name = backend.get("name", "unknown")
        backend_properties = backend.get("properties", {})
        yield f"Backend {backend_name} URL", str(backend_properties.get("url", ""))
        yield f"Backend {backend_name} credentials", json.dumps(backend_properties.get("credentials", {}), sort_keys=True)


def validate_snapshot(snapshot: dict[str, Any]) -> list[Finding]:
    api_properties = snapshot.get("api", {}).get("properties", {})
    if api_properties.get("apiType") == "mcp" or api_properties.get("type") == "mcp":
        return validate_mcp_snapshot(snapshot)

    findings: list[Finding] = []
    all_text = list(iter_text(snapshot))

    for location, value in all_text:
        for description, pattern in LEGACY_PATTERNS.items():
            if pattern.search(value):
                findings.append(Finding("ERROR", description, f"{location} contains legacy configuration: {value[:180]}"))

    combined_policy = "\n".join(value for location, value in all_text if "policy" in location.lower())
    authentication_text = "\n".join(value for location, value in all_text if "policy" in location.lower() or "credentials" in location.lower())
    combined_routes = "\n".join(value for location, value in all_text if "policy" not in location.lower())
    if "/openai/v1" not in combined_routes.lower() and "/openai/v1" not in combined_policy.lower():
        findings.append(Finding("ERROR", "OpenAI v1 route", "No /openai/v1 route or rewrite was found."))

    if "authentication-managed-identity" not in authentication_text and "api-key" not in authentication_text.lower():
        findings.append(
            Finding(
                "WARNING",
                "backend authentication",
                "No managed identity policy or api-key header was detected. Confirm backend authentication.",
            )
        )

    operations = snapshot.get("operations", [])
    has_chat_operation = any(
        "chat/completions" in str(item.get("operation", {}).get("properties", {}).get("urlTemplate", "")).lower()
        for item in operations
    )
    if not has_chat_operation:
        findings.append(Finding("ERROR", "chat completions operation", "No chat/completions operation was found."))

    if not findings:
        findings.append(Finding("PASS", "OpenAI v1 APIM configuration", "No incompatible configuration was detected."))
    return findings


def validate_mcp_snapshot(snapshot: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    properties = snapshot.get("api", {}).get("properties", {})
    mcp_properties = properties.get("mcpProperties", {})
    transport_type = mcp_properties.get("transportType")
    endpoints = mcp_properties.get("endpoints")

    if transport_type != "streamable":
        findings.append(
            Finding(
                "ERROR",
                "MCP transport contract",
                "APIM did not retain mcpProperties.transportType='streamable'. This indicates the regional APIM stamp has not adopted the documented 2025-09-01-preview passthrough contract.",
            )
        )

    if not isinstance(endpoints, list):
        findings.append(
            Finding(
                "ERROR",
                "MCP endpoint contract",
                "mcpProperties.endpoints is not the documented array shape. A dictionary is a temporary live-stamp compatibility shape and does not prove passthrough tool discovery works.",
            )
        )
    elif not any(
        endpoint.get("name") == "message" and endpoint.get("uriTemplate") == "/mcp"
        for endpoint in endpoints
        if isinstance(endpoint, dict)
    ):
        findings.append(Finding("ERROR", "MCP message endpoint", "No message endpoint with uriTemplate '/mcp' was found."))

    runtime_tools = snapshot.get("runtimeMcpTools")
    if runtime_tools is not None:
        advertised_tools = {str(tool) for tool in runtime_tools}
        if not advertised_tools:
            findings.append(
                Finding(
                    "ERROR",
                    "MCP tool discovery",
                    "APIM initialized the MCP session but tools/list returned zero tools. Validate the same backend directly; if it advertises tools, open an Azure support case for the APIM preview rollout.",
                )
            )
        else:
            missing_tools = EXPECTED_MCP_TOOLS - advertised_tools
            unexpected_tools = advertised_tools - EXPECTED_MCP_TOOLS
            if missing_tools or unexpected_tools:
                details = []
                if missing_tools:
                    details.append(f"missing: {', '.join(sorted(missing_tools))}")
                if unexpected_tools:
                    details.append(f"unexpected: {', '.join(sorted(unexpected_tools))}")
                findings.append(Finding("ERROR", "MCP tool contract", "; ".join(details)))

    if not findings:
        findings.append(Finding("PASS", "MCP APIM configuration", "The APIM MCP resource contract is complete."))
    return findings


async def discover_mcp_tools(url: str, subscription_key: str) -> list[str]:
    headers = {"Ocp-Apim-Subscription-Key": subscription_key}
    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
    return [tool.name for tool in result.tools]


def print_report(findings: list[Finding]) -> None:
    for finding in findings:
        print(f"[{finding.severity}] {finding.check}: {finding.message}")
    errors = sum(finding.severity == "ERROR" for finding in findings)
    warnings = sum(finding.severity == "WARNING" for finding in findings)
    print(f"\nSummary: {errors} error(s), {warnings} warning(s)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path, help="Validate a previously exported APIM JSON snapshot.")
    source.add_argument("--resource-group", help="APIM resource group.")
    parser.add_argument("--service-name", help="APIM service name.")
    parser.add_argument("--api-id", help="APIM API identifier (not display name).")
    parser.add_argument("--export", type=Path, help="Write the sanitized APIM snapshot to this file.")
    parser.add_argument("--mcp-url", help="Run MCP initialize and tools/list against this APIM URL.")
    parser.add_argument(
        "--mcp-key-env",
        default="APIM_SUBSCRIPTION_KEY",
        help="Environment variable containing the APIM subscription key (default: APIM_SUBSCRIPTION_KEY).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        missing = [name for name in ("service_name", "api_id") if not getattr(args, name)]
        if missing:
            print(f"Missing required argument(s): {', '.join('--' + name.replace('_', '-') for name in missing)}", file=sys.stderr)
            return 2
        snapshot = get_apim_snapshot(args.resource_group, args.service_name, args.api_id)

    if args.mcp_url:
        subscription_key = os.environ.get(args.mcp_key_env)
        if not subscription_key:
            print(f"Environment variable {args.mcp_key_env} is required with --mcp-url.", file=sys.stderr)
            return 2
        snapshot["runtimeMcpTools"] = asyncio.run(discover_mcp_tools(args.mcp_url, subscription_key))

    if args.export:
        args.export.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    findings = validate_snapshot(snapshot)
    print_report(findings)
    return 1 if any(finding.severity == "ERROR" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())