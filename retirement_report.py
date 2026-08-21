#!/usr/bin/env python3
"""Produce fail-closed evidence for retiring the legacy Azure OpenAI route."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WINDOWS = (7, 14, 30)
ROUTING_MESSAGE = "OpenAI migration request routed by API mode."


def build_query() -> str:
    summaries = []
    for days in WINDOWS:
        summaries.append(
            f"""(events
| where timestamp > ago({days}d)
| summarize request_count=count(), observed_request_count=countif(isnotempty(resultCode)), failed_count=countif(success == false), throttled_count=countif(resultCode == \"429\"), p50_ms=percentile(durationMs, 50), p95_ms=percentile(durationMs, 95), p99_ms=percentile(durationMs, 99), last_request=max(timestamp) by api_mode
| extend window_days={days})"""
        )
    return f"""let routes = traces
| where timestamp > ago(30d)
| where message == \"{ROUTING_MESSAGE}\"
| extend api_mode=tostring(customDimensions.api_mode)
| project operation_Id, api_mode, timestamp;
let requestData = requests
| where timestamp > ago(30d)
| project operation_Id, success, resultCode=tostring(resultCode), durationMs=duration / 1ms;
let events = routes
| join kind=leftouter requestData on operation_Id
| project timestamp, api_mode, success, resultCode, durationMs;
union {', '.join(summaries)}
| order by window_days asc, api_mode asc"""


def parse_app_insights_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    names = [column["name"] for column in table.get("columns", [])]
    return [dict(zip(names, row, strict=False)) for row in table.get("rows", [])]


def mode_metrics(rows: list[dict[str, Any]], window_days: int, mode: str) -> dict[str, Any]:
    matching = [
        row for row in rows
        if int(row.get("window_days", 0)) == window_days and row.get("api_mode") == mode
    ]
    if not matching:
        return {
            "requests": 0,
            "observed_requests": 0,
            "failed": 0,
            "throttled": 0,
            "success_rate": None,
            "throttle_rate": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "last_request": None,
        }
    row = matching[0]
    requests = int(row.get("request_count") or 0)
    observed = int(row.get("observed_request_count") or 0)
    failed = int(row.get("failed_count") or 0)
    throttled = int(row.get("throttled_count") or 0)
    return {
        "requests": requests,
        "observed_requests": observed,
        "failed": failed,
        "throttled": throttled,
        "success_rate": round((observed - failed) / observed, 6) if observed else None,
        "throttle_rate": round(throttled / observed, 6) if observed else None,
        "p50_ms": row.get("p50_ms"),
        "p95_ms": row.get("p95_ms"),
        "p99_ms": row.get("p99_ms"),
        "last_request": row.get("last_request"),
    }


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate(
    rows: list[dict[str, Any]],
    legacy_p95_baseline_ms: float | None,
    min_success_rate: float = 0.995,
    max_latency_increase: float = 0.10,
    rollback_rehearsed: bool = False,
    parity_passed: bool = False,
    owner_approved: bool = False,
    min_v1_requests: int = 100,
    max_v1_last_request_age_hours: int = 24,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    now = (evaluated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windows: dict[str, Any] = {}
    for days in WINDOWS:
        legacy = mode_metrics(rows, days, "legacy")
        v1 = mode_metrics(rows, days, "v1")
        default_v1 = mode_metrics(rows, days, "default-v1")
        combined_v1 = {
            "requests": v1["requests"] + default_v1["requests"],
            "observed_requests": v1["observed_requests"] + default_v1["observed_requests"],
            "failed": v1["failed"] + default_v1["failed"],
            "throttled": v1["throttled"] + default_v1["throttled"],
        }
        observed = combined_v1["observed_requests"]
        combined_v1["success_rate"] = round((observed - combined_v1["failed"]) / observed, 6) if observed else None
        combined_v1["throttle_rate"] = round(combined_v1["throttled"] / observed, 6) if observed else None
        last_requests = [
            timestamp
            for timestamp in (parse_timestamp(v1["last_request"]), parse_timestamp(default_v1["last_request"]))
            if timestamp is not None
        ]
        combined_v1["last_request"] = max(last_requests).isoformat() if last_requests else None
        windows[str(days)] = {
            "legacy": legacy,
            "v1": v1,
            "default_v1": default_v1,
            "combined_v1": combined_v1,
        }

    evidence = windows["14"]
    combined = evidence["combined_v1"]
    v1_p95_values = [
        value for value in (evidence["v1"]["p95_ms"], evidence["default_v1"]["p95_ms"]) if value is not None
    ]
    observed_p95 = max(v1_p95_values) if v1_p95_values else None
    last_v1_request = parse_timestamp(combined["last_request"])
    recent_v1_traffic = (
        last_v1_request is not None
        and timedelta(0) <= now - last_v1_request <= timedelta(hours=max_v1_last_request_age_hours)
    )
    telemetry_complete = (
        combined["requests"] >= min_v1_requests
        and combined["observed_requests"] == combined["requests"]
        and evidence["legacy"]["observed_requests"] == evidence["legacy"]["requests"]
    )
    latency_passed = (
        legacy_p95_baseline_ms is not None
        and observed_p95 is not None
        and observed_p95 <= legacy_p95_baseline_ms * (1 + max_latency_increase)
    )
    checks = {
        "telemetry_complete": telemetry_complete,
        "minimum_v1_request_volume": combined["requests"] >= min_v1_requests,
        "recent_v1_traffic": recent_v1_traffic,
        "no_legacy_requests_for_14_days": evidence["legacy"]["requests"] == 0,
        "v1_success_rate": combined["success_rate"] is not None and combined["success_rate"] >= min_success_rate,
        "v1_p95_within_approved_baseline": latency_passed,
        "rollback_rehearsed": rollback_rehearsed,
        "parity_passed": parity_passed,
        "owner_approved": owner_approved,
    }
    return {
        "generated_at": now.isoformat(),
        "ready": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "min_v1_success_rate": min_success_rate,
            "min_v1_requests": min_v1_requests,
            "max_v1_last_request_age_hours": max_v1_last_request_age_hours,
            "legacy_p95_baseline_ms": legacy_p95_baseline_ms,
            "max_latency_increase": max_latency_increase,
            "max_v1_p95_ms": (
                round(legacy_p95_baseline_ms * (1 + max_latency_increase), 3)
                if legacy_p95_baseline_ms is not None else None
            ),
            "observed_v1_p95_ms": observed_p95,
        },
        "windows": windows,
    }


def find_azure_cli() -> str:
    for command in ("az", "az.cmd"):
        path = shutil.which(command)
        if path:
            return path
    raise RuntimeError("Azure CLI was not found on PATH.")


def query_azure(subscription_id: str, resource_group: str, component_name: str) -> list[dict[str, Any]]:
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Insights/components/{component_name}"
    )
    body = json.dumps({"query": build_query()})
    command = [
        find_azure_cli(), "rest", "--method", "post",
        "--uri", f"https://management.azure.com{resource_id}/query?api-version=2018-04-20",
        "--headers", "Content-Type=application/json", "--body", body,
        "--subscription", subscription_id, "--output", "json", "--only-show-errors",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return parse_app_insights_response(json.loads(result.stdout))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Application Insights query response JSON for offline evaluation.")
    source.add_argument("--application-insights-name")
    parser.add_argument("--subscription-id")
    parser.add_argument("--resource-group")
    parser.add_argument("--legacy-p95-baseline-ms", type=float)
    parser.add_argument("--min-success-rate", type=float, default=0.995)
    parser.add_argument("--min-v1-requests", type=int, default=100)
    parser.add_argument("--max-v1-last-request-age-hours", type=int, default=24)
    parser.add_argument("--max-latency-increase", type=float, default=0.10)
    parser.add_argument("--rollback-rehearsed", action="store_true")
    parser.add_argument("--parity-passed", action="store_true")
    parser.add_argument("--owner-approved", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.input:
            rows = parse_app_insights_response(json.loads(args.input.read_text(encoding="utf-8-sig")))
        else:
            if not args.subscription_id or not args.resource_group:
                raise ValueError("--subscription-id and --resource-group are required for a live query.")
            rows = query_azure(args.subscription_id, args.resource_group, args.application_insights_name)
        report = evaluate(
            rows,
            legacy_p95_baseline_ms=args.legacy_p95_baseline_ms,
            min_success_rate=args.min_success_rate,
            max_latency_increase=args.max_latency_increase,
            rollback_rehearsed=args.rollback_rehearsed,
            parity_passed=args.parity_passed,
            owner_approved=args.owner_approved,
            min_v1_requests=args.min_v1_requests,
            max_v1_last_request_age_hours=args.max_v1_last_request_age_hours,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(json.dumps({"ready": False, "error_type": type(error).__name__}), file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if args.require_ready and not report["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
