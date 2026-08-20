#!/usr/bin/env python3
"""Compare legacy Azure AI model-inference behavior with OpenAI API v1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError
from openai import OpenAIError

from smoke_test import ChatResult, Target, invoke_chat


def legacy_chat(target: Target, prompt: str, max_tokens: int = 80, request_options: dict[str, Any] | None = None) -> ChatResult:
    return invoke_chat("legacy", target, prompt, max_tokens, request_options)


def v1_chat(target: Target, prompt: str, max_tokens: int = 80, request_options: dict[str, Any] | None = None) -> ChatResult:
    return invoke_chat("v1", target, prompt, max_tokens, request_options)


def normalize(result: ChatResult) -> dict[str, Any]:
    return {
        "output_nonempty": bool(result.content.strip()),
        "finish_reason": result.finish_reason,
        "tool_call_count": result.tool_call_count,
        "output_length": len(result.content),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def compare(legacy: dict[str, Any], current: dict[str, Any], max_length_ratio: float) -> dict[str, Any]:
    legacy_length = legacy["output_length"]
    current_length = current["output_length"]
    ratio = max(legacy_length, current_length) / max(1, min(legacy_length, current_length))
    checks = {
        "nonempty_match": legacy["output_nonempty"] == current["output_nonempty"],
        "finish_reason_match": legacy["finish_reason"] == current["finish_reason"],
        "tool_call_count_match": legacy["tool_call_count"] == current["tool_call_count"],
        "length_ratio_within_limit": ratio <= max_length_ratio,
    }
    return {"passed": all(checks.values()), "checks": checks, "output_length_ratio": round(ratio, 2)}


def load_corpus(path: Path) -> list[dict[str, Any]]:
    scenarios = []
    identifiers = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenario = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON on corpus line {line_number}.") from error
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise ValueError(f"Corpus line {line_number} must contain a string id.")
        if not isinstance(scenario.get("prompt"), str):
            raise ValueError(f"Corpus line {line_number} must contain a string prompt.")
        if scenario["id"] in identifiers:
            raise ValueError(f"Duplicate corpus scenario id: {scenario['id']}.")
        identifiers.add(scenario["id"])
        scenarios.append(scenario)
    if not scenarios:
        raise ValueError("Corpus must contain at least one scenario.")
    return scenarios


def compare_corpus(
    target: Target,
    scenarios: list[dict[str, Any]],
    default_max_length_ratio: float,
    min_pass_rate: float,
) -> dict[str, Any]:
    if not 0 <= min_pass_rate <= 1:
        raise ValueError("Minimum pass rate must be between 0 and 1.")
    results = []
    for scenario in scenarios:
        max_tokens = int(scenario.get("max_tokens", 80))
        request_options: dict[str, Any] = {
            key: scenario[key]
            for key in ("tools", "tool_choice", "response_format")
            if key in scenario
        }
        legacy = normalize(legacy_chat(target, scenario["prompt"], max_tokens, request_options))
        current = normalize(v1_chat(target, scenario["prompt"], max_tokens, request_options))
        comparison = compare(
            legacy,
            current,
            float(scenario.get("max_length_ratio", default_max_length_ratio)),
        )
        expected_checks = {}
        for field in ("finish_reason", "tool_call_count"):
            expected_key = f"expected_{field}"
            if expected_key in scenario:
                expected_checks[expected_key] = (
                    legacy[field] == scenario[expected_key] and current[field] == scenario[expected_key]
                )
        comparison["checks"].update(expected_checks)
        comparison["passed"] = comparison["passed"] and all(expected_checks.values())
        results.append(
            {
                "id": scenario["id"],
                "passed": comparison["passed"],
                "comparison": comparison,
                "legacy": legacy,
                "v1": current,
            }
        )
    passed_count = sum(result["passed"] for result in results)
    pass_rate = passed_count / len(results)
    return {
        "passed": pass_rate >= min_pass_rate,
        "scenario_count": len(results),
        "passed_count": passed_count,
        "pass_rate": round(pass_rate, 4),
        "minimum_pass_rate": min_pass_rate,
        "scenarios": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), default="direct")
    parser.add_argument("--prompt", default="Return only the word ready.")
    parser.add_argument("--corpus", type=Path, help="JSONL parity corpus; supersedes --prompt.")
    parser.add_argument("--max-length-ratio", type=float, default=2.0)
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        corpus = getattr(args, "corpus", None)
        if corpus:
            report = compare_corpus(
                args.target,
                load_corpus(corpus),
                args.max_length_ratio,
                getattr(args, "min_pass_rate", 1.0),
            )
        else:
            report = compare(
                normalize(legacy_chat(args.target, args.prompt)),
                normalize(v1_chat(args.target, args.prompt)),
                args.max_length_ratio,
            )
    except (OSError, ValueError, AzureError, OpenAIError) as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        return 1
    rendered = json.dumps(report, indent=2 if corpus else None, sort_keys=True)
    output = getattr(args, "output", None)
    if output:
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())