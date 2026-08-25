#!/usr/bin/env python3
"""Test user_security_context against the lab DeepSeek deployment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import user_security_context_test

DEFAULT_DEEPSEEK_DEPLOYMENT = "DeepSeek-V4-Flash"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="Return only: DeepSeek user security context accepted")
    parser.add_argument(
        "--print-full-exchange",
        action="store_true",
        help="Print complete request/response JSON bodies. This includes user identity and IP data.",
    )
    parser.add_argument(
        "--acknowledge-sensitive-output",
        action="store_true",
        help="Confirm that sensitive request/response data may be written to the console or a file.",
    )
    parser.add_argument(
        "--save-full-exchange",
        type=Path,
        metavar="FILE",
        help="Save complete request/response JSON bodies to FILE. This includes user identity and IP data.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = os.getenv("AZURE_OPENAI_DEEPSEEK_BASE_URL")
    if not base_url:
        raise ValueError("AZURE_OPENAI_DEEPSEEK_BASE_URL is required.")

    os.environ["AZURE_OPENAI_BASE_URL"] = base_url
    tenant_id = os.getenv("AZURE_OPENAI_DEEPSEEK_TENANT_ID")
    if tenant_id:
        os.environ["AZURE_OPENAI_TENANT_ID"] = tenant_id
    else:
        os.environ.pop("AZURE_OPENAI_TENANT_ID", None)
    os.environ["AZURE_OPENAI_DEPLOYMENT"] = os.getenv(
        "AZURE_OPENAI_DEEPSEEK_DEPLOYMENT",
        DEFAULT_DEEPSEEK_DEPLOYMENT,
    )
    args.target = "direct"
    return user_security_context_test.main(args)


if __name__ == "__main__":
    raise SystemExit(main())