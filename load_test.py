#!/usr/bin/env python3
"""Run bounded v1 and legacy concurrency, latency, token, and estimated-cost tests."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from smoke_test import ApiMode, invoke_chat, require_env

MAX_REQUESTS_PER_MODE = 10_000
LARGE_LOAD_THRESHOLD = 1_000


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def invoke(target: str, prompt: str, api_mode: ApiMode = "v1") -> dict[str, Any]:
    started = time.perf_counter()
    result = invoke_chat(api_mode, target, prompt, max_tokens=40)
    return {
        "latency_ms": (time.perf_counter() - started) * 1000,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def estimate_cost(input_tokens: int, output_tokens: int) -> float | None:
    input_rate = os.getenv("OPENAI_INPUT_USD_PER_1M_TOKENS")
    output_rate = os.getenv("OPENAI_OUTPUT_USD_PER_1M_TOKENS")
    if input_rate is None or output_rate is None:
        return None
    return input_tokens * float(input_rate) / 1_000_000 + output_tokens * float(output_rate) / 1_000_000


def validate_configuration(target: str, modes: tuple[ApiMode, ...]) -> None:
    require_env("AZURE_OPENAI_DEPLOYMENT")
    if target == "apim":
        require_env("APIM_OPENAI_BASE_URL")
        require_env("APIM_SUBSCRIPTION_KEY")
        return
    if "v1" in modes:
        require_env("AZURE_OPENAI_BASE_URL")
    if "legacy" in modes:
        require_env("LEGACY_MODELS_BASE_URL")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), required=True)
    parser.add_argument("--api-mode", choices=("v1", "legacy", "both"), default="v1")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--prompt", default="Return only the word ready.")
    parser.add_argument(
        "--confirm-large-load",
        action="store_true",
        help="Confirm loads above 1,000 requests per mode.",
    )
    return parser.parse_args()


def run_load(target: str, api_mode: ApiMode, requests: int, concurrency: int, prompt: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: dict[str, int] = {}
    failure_examples: dict[str, str] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(invoke, target, prompt, api_mode) for _ in range(requests)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                error_name = type(error).__name__
                failures[error_name] = failures.get(error_name, 0) + 1
                failure_examples.setdefault(error_name, str(error)[:300])

    elapsed = time.perf_counter() - started
    latencies = [result["latency_ms"] for result in results]
    input_tokens = sum(result["input_tokens"] for result in results)
    output_tokens = sum(result["output_tokens"] for result in results)
    report = {
        "target": target,
        "api_mode": api_mode,
        "requested": requests,
        "succeeded": len(results),
        "failed": requests - len(results),
        "failures_by_type": failures,
        "failure_examples": failure_examples,
        "throughput_rps": round(len(results) / elapsed, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost(input_tokens, output_tokens),
    }
    if latencies:
        report.update({
            "latency_mean_ms": round(statistics.mean(latencies)),
            "latency_p50_ms": round(percentile(latencies, 0.50)),
            "latency_p95_ms": round(percentile(latencies, 0.95)),
            "latency_p99_ms": round(percentile(latencies, 0.99)),
        })
    return report


def main() -> int:
    args = parse_args()
    if not 1 <= args.requests <= MAX_REQUESTS_PER_MODE:
        raise SystemExit(f"--requests must be between 1 and {MAX_REQUESTS_PER_MODE}")
    if args.requests > LARGE_LOAD_THRESHOLD and not args.confirm_large_load:
        raise SystemExit("loads above 1,000 requests per mode require --confirm-large-load")
    if not 1 <= args.concurrency <= min(args.requests, 100):
        raise SystemExit("--concurrency must be between 1 and min(requests, 100)")

    modes: tuple[ApiMode, ...] = ("v1", "legacy") if args.api_mode == "both" else (args.api_mode,)
    try:
        validate_configuration(args.target, modes)
    except ValueError as error:
        print(f"Load test configuration error: {error}", file=sys.stderr)
        return 2
    reports = [run_load(args.target, mode, args.requests, args.concurrency, args.prompt) for mode in modes]
    output = {
        "target": args.target,
        "api_mode": "both",
        "reports": reports,
    } if args.api_mode == "both" else reports[0]
    print(json.dumps(output, sort_keys=True))
    return 0 if all(report["failed"] == 0 for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())