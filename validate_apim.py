#!/usr/bin/env python3
"""Validate APIM configuration for an Azure OpenAI /openai/v1 migration."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LEGACY_PATTERNS = {
    "azure-ai-inference /models route": re.compile(r"(?:services\.ai\.azure\.com)?/models(?:/|\b)", re.I),
    "dated api-version": re.compile(r"api-version(?:=|&amp;|%3[dD])", re.I),
    "deployment-based OpenAI route": re.compile(r"/openai/deployments/", re.I),
    "legacy Microsoft Entra scope": re.compile(r"https://cognitiveservices\.azure\.com(?:/\.default)?", re.I),
}

UNIFIED_CHAT_POLICY_MARKERS = (
    "x-api-mode",
    "string.isnullorempty",
    "openai-v1-migration",
    "api_mode",
    "request_id",
    "/openai/v1/chat/completions",
    "/openai/deployments/",
    "chat/completions?api-version=2024-10-21",
    "https://ai.azure.com",
    "https://cognitiveservices.azure.com",
    "invalid_api_mode",
)


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
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=environment, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Azure CLI failed: {' '.join(command)}")
    return json.loads(result.stdout.lstrip("\ufeff"))


def get_resource(resource_id: str, api_version: str = "2024-05-01") -> dict[str, Any]:
    return run_az_json(["rest", "--method", "get", "--url", f"{resource_id}?api-version={api_version}"])


def get_policy(
    resource_id: str, api_version: str = "2024-05-01", subscription_id: str | None = None
) -> dict[str, Any]:
    token_arguments = ["account", "get-access-token", "--resource", "https://management.azure.com/"]
    if subscription_id:
        token_arguments.extend(["--subscription", subscription_id])
    token = run_az_json(token_arguments)["accessToken"]
    request = urllib.request.Request(
        f"https://management.azure.com{resource_id}?api-version={api_version}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"ARM request failed with HTTP {error.code}") from error


def is_resource_not_found(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "404" in message or "resourcenotfound" in message


def get_apim_snapshot(
    resource_group: str, service_name: str, api_id: str, subscription_id: str | None = None
) -> dict[str, Any]:
    subscription_id = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID") or run_az_json(
        ["account", "show", "--query", "id"]
    )
    service_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{service_name}"
    )
    api_resource_id = f"{service_id}/apis/{api_id}"
    api = get_resource(api_resource_id)
    try:
        api_policy = get_policy(f"{api_resource_id}/policies/policy", "2024-05-01", subscription_id)
    except RuntimeError as error:
        if not is_resource_not_found(error):
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
            policy = get_policy(policy_id, subscription_id=subscription_id)
        except RuntimeError as error:
            if not is_resource_not_found(error):
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
            "value[].{name:name,properties:{displayName:properties.displayName,secret:properties.secret}}",
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
    findings: list[Finding] = []
    all_text = list(iter_text(snapshot))

    for location, value in all_text:
        normalized_value = value.lower()
        is_unified_chat_policy = "policy" in location.lower() and all(
            marker in normalized_value for marker in UNIFIED_CHAT_POLICY_MARKERS
        )
        if is_unified_chat_policy:
            continue
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

    for item in operations:
        operation = item.get("operation", {})
        url_template = str(operation.get("properties", {}).get("urlTemplate", "")).lower()
        policy = str(item.get("policy", {}).get("properties", {}).get("value", ""))
        normalized_policy = policy.lower()
        if url_template == "/v1/chat/completions" and "x-api-mode" in normalized_policy:
            missing_markers = [marker for marker in UNIFIED_CHAT_POLICY_MARKERS if marker not in normalized_policy]
            if missing_markers:
                findings.append(
                    Finding(
                        "ERROR",
                        "dual-mode chat operation",
                        f"Dual-mode chat policy is missing: {', '.join(missing_markers)}.",
                    )
                )

    if not findings:
        findings.append(Finding("PASS", "OpenAI v1 APIM configuration", "No incompatible configuration was detected."))
    return findings


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
    parser.add_argument("--subscription-id", help="Azure subscription containing the APIM service.")
    parser.add_argument("--export", type=Path, help="Write the sanitized APIM snapshot to this file.")
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
        snapshot = get_apim_snapshot(args.resource_group, args.service_name, args.api_id, args.subscription_id)

    if args.export:
        args.export.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    findings = validate_snapshot(snapshot)
    print_report(findings)
    return 1 if any(finding.severity == "ERROR" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())