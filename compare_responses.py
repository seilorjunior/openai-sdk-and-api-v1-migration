#!/usr/bin/env python3
"""Compare legacy Azure AI model-inference behavior with OpenAI API v1."""

from __future__ import annotations

import argparse
import json
from typing import Any

from azure.core.exceptions import AzureError
from openai import OpenAIError

from smoke_test import ChatResult, Target, invoke_chat


def legacy_chat(target: Target, prompt: str) -> ChatResult:
    return invoke_chat("legacy", target, prompt)


def v1_chat(target: Target, prompt: str) -> ChatResult:
    return invoke_chat("v1", target, prompt)


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), default="direct")
    parser.add_argument("--prompt", default="Return only the word ready.")
    parser.add_argument("--max-length-ratio", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = compare(
            normalize(legacy_chat(args.target, args.prompt)),
            normalize(v1_chat(args.target, args.prompt)),
            args.max_length_ratio,
        )
    except (ValueError, AzureError, OpenAIError) as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())