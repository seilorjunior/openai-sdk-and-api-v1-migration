#!/usr/bin/env python3
"""Run bounded v1 and legacy concurrency, latency, token, and estimated-cost tests."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from azure.core.exceptions import AzureError, ServiceRequestError, ServiceResponseError
from openai import APIConnectionError, APIStatusError, APITimeoutError

from smoke_test import ApiMode, build_chat_client, require_env, send_chat

MAX_REQUESTS_PER_MODE = 10_000
LARGE_LOAD_THRESHOLD = 1_000
MAX_WARMUP_REQUESTS = 100

# Failures caused by the network/transport layer (connection resets, timeouts,
# DNS failures) are reported separately from failures caused by the request
# itself (HTTP error responses, invalid configuration) so load-test operators
# can tell infrastructure flakiness apart from real regressions.
TRANSPORT_FAILURE_TYPES = (
    APIConnectionError,
    APITimeoutError,
    ServiceRequestError,
    ServiceResponseError,
    ConnectionError,
    TimeoutError,
)
REQUEST_FAILURE_TYPES = (APIStatusError, AzureError, ValueError)

_thread_local = threading.local()


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def classify_failure(error: BaseException) -> str:
    if isinstance(error, TRANSPORT_FAILURE_TYPES):
        return "transport"
    if isinstance(error, REQUEST_FAILURE_TYPES):
        return "request"
    return "other"


def get_thread_client(target: str, api_mode: ApiMode) -> Any:
    """Return a client cached on the current worker thread, building it once.

    Reusing one client per worker thread (instead of building a new client for
    every request) avoids measuring connection/authentication setup on every
    call and keeps the load test focused on request latency.
    """

    cache = getattr(_thread_local, "clients", None)
    if cache is None:
        cache = {}
        _thread_local.clients = cache
    client = cache.get(api_mode)
    if client is None:
        client = build_chat_client(api_mode, target)
        cache[api_mode] = client
    return client


def invoke(target: str, prompt: str, api_mode: ApiMode = "v1") -> dict[str, Any]:
    model = require_env("AZURE_OPENAI_DEPLOYMENT")
    client = get_thread_client(target, api_mode)
    started = time.perf_counter()
    result = send_chat(client, api_mode, target, model, prompt, max_tokens=40)
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
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=0,
        help=f"Unmeasured requests per mode to run before timing (0-{MAX_WARMUP_REQUESTS}, default 0).",
    )
    return parser.parse_args()


def run_warmup(target: str, api_mode: ApiMode, warmup_requests: int, concurrency: int, prompt: str) -> None:
    if warmup_requests <= 0:
        return
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(invoke, target, prompt, api_mode) for _ in range(warmup_requests)]
        for future in as_completed(futures):
            future.exception()  # Discard warm-up successes and failures alike.


def run_load(target: str, api_mode: ApiMode, requests: int, concurrency: int, prompt: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures_by_type: dict[str, int] = {}
    failures_by_category: dict[str, int] = {}
    failure_examples: dict[str, str] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(invoke, target, prompt, api_mode) for _ in range(requests)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                error_name = type(error).__name__
                failures_by_type[error_name] = failures_by_type.get(error_name, 0) + 1
                category = classify_failure(error)
                failures_by_category[category] = failures_by_category.get(category, 0) + 1
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
        "failures_by_type": failures_by_type,
        "failures_by_category": failures_by_category,
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
    if not 0 <= args.warmup_requests <= MAX_WARMUP_REQUESTS:
        raise SystemExit(f"--warmup-requests must be between 0 and {MAX_WARMUP_REQUESTS}")

    modes: tuple[ApiMode, ...] = ("v1", "legacy") if args.api_mode == "both" else (args.api_mode,)
    try:
        validate_configuration(args.target, modes)
    except ValueError as error:
        print(f"Load test configuration error: {error}", file=sys.stderr)
        return 2
    for mode in modes:
        run_warmup(args.target, mode, args.warmup_requests, args.concurrency, args.prompt)
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
