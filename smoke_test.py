#!/usr/bin/env python3
"""Call Azure OpenAI v1 or legacy model inference using the matching official SDK."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from azure.ai.inference import ChatCompletionsClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential, DefaultAzureCredential, get_bearer_token_provider
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAI

ApiMode = Literal["v1", "legacy"]
Target = Literal["direct", "apim"]


@dataclass(frozen=True)
class ChatResult:
    model: str
    content: str
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    tool_call_count: int


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Set the {name} environment variable.")
    return value


def client_options(target: str, api_mode: ApiMode | None = None) -> dict[str, object]:
    timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
    if target == "direct":
        tenant_id = os.getenv("AZURE_OPENAI_TENANT_ID")
        credential = AzureCliCredential(tenant_id=tenant_id) if tenant_id else DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://ai.azure.com/.default",
        )
        return {
            "api_key": token_provider,
            "base_url": require_env("AZURE_OPENAI_BASE_URL").rstrip("/") + "/",
            "timeout": timeout,
            "max_retries": max_retries,
        }

    headers = {"Ocp-Apim-Subscription-Key": require_env("APIM_SUBSCRIPTION_KEY")}
    if api_mode is not None:
        headers["X-API-Mode"] = api_mode
    return {
        "api_key": os.getenv("APIM_CLIENT_API_KEY", "apim-managed-backend-auth"),
        "base_url": require_env("APIM_OPENAI_BASE_URL").rstrip("/") + "/",
        "default_headers": headers,
        "timeout": timeout,
        "max_retries": max_retries,
    }


def build_client(target: str) -> OpenAI:
    return OpenAI(**cast(Any, client_options(target)))


def build_chat_client(api_mode: ApiMode | None, target: Target) -> OpenAI | ChatCompletionsClient:
    if target == "apim":
        return OpenAI(**cast(Any, client_options(target, api_mode)))
    if api_mode is None:
        raise ValueError("Default API mode can only be tested through APIM.")
    if api_mode == "v1":
        return build_client(target)
    return ChatCompletionsClient(
        endpoint=require_env("LEGACY_MODELS_BASE_URL").rstrip("/"),
        credential=DefaultAzureCredential(),
        credential_scopes=["https://cognitiveservices.azure.com/.default"],
    )


def send_chat(
    client: OpenAI | ChatCompletionsClient,
    api_mode: ApiMode | None,
    target: Target,
    model: str,
    prompt: str,
    max_tokens: int = 80,
    request_options: dict[str, Any] | None = None,
) -> ChatResult:
    """Send one chat request through an already-built client and return the common result.

    Callers that issue many requests (for example, load_test.py) should build the
    client once with build_chat_client() and call this function per request instead
    of invoke_chat(), which builds a new client every time.
    """

    messages = cast(Any, [{"role": "user", "content": prompt}])
    response: Any
    completion_options = {"max_tokens": max_tokens, "temperature": 0, **(request_options or {})}
    if api_mode == "legacy" and target == "direct":
        legacy_client = cast(ChatCompletionsClient, client)
        response = legacy_client.complete(
            model=model,
            messages=messages,
            **cast(Any, completion_options),
        )
    else:
        v1_client = cast(OpenAI, client)
        response = v1_client.chat.completions.create(
            model=model,
            messages=messages,
            **cast(Any, completion_options),
        )

    choice = response.choices[0]
    usage = response.usage
    tool_calls = getattr(choice.message, "tool_calls", None) or []
    return ChatResult(
        model=response.model or model,
        content=choice.message.content or "",
        finish_reason=choice.finish_reason,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        tool_call_count=len(tool_calls),
    )


def invoke_chat(
    api_mode: ApiMode | None,
    target: Target,
    prompt: str,
    max_tokens: int = 80,
    request_options: dict[str, Any] | None = None,
) -> ChatResult:
    client = build_chat_client(api_mode, target)
    model = require_env("AZURE_OPENAI_DEPLOYMENT")
    return send_chat(client, api_mode, target, model, prompt, max_tokens, request_options)


async def cancellation_probe(target: str, cancel_after_seconds: float) -> bool:
    client = AsyncOpenAI(**cast(Any, client_options(target)))
    task = asyncio.create_task(
        client.responses.create(
            model=require_env("AZURE_OPENAI_DEPLOYMENT"),
            input="Return a concise health check.",
            max_output_tokens=200,
        )
    )
    await asyncio.sleep(cancel_after_seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return True
    finally:
        await client.close()
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), required=True)
    parser.add_argument("--api-mode", choices=("default", "v1", "legacy"), default="v1")
    parser.add_argument("--prompt", default="Responda somente: POC SDK OpenAI v1 OK")
    parser.add_argument("--cancel-after", type=float, help="Cancel a Responses API call after this many seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_mode = None if args.api_mode == "default" else args.api_mode
    try:
        if args.cancel_after is not None:
            if args.api_mode != "v1":
                raise ValueError("Cancellation probing uses the v1 Responses API and is unavailable in legacy mode.")
            cancelled = asyncio.run(cancellation_probe(args.target, args.cancel_after))
            print(f"target={args.target}")
            print(f"api_mode={args.api_mode}")
            print(f"cancelled={str(cancelled).lower()}")
            return 0 if cancelled else 1
        started = time.perf_counter()
        result = invoke_chat(api_mode, args.target, args.prompt, max_tokens=40)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"target={args.target}")
        print(f"api_mode={args.api_mode}")
        print(f"model={result.model}")
        print(f"latency_ms={elapsed_ms:.0f}")
        print(f"response_present={str(bool(result.content.strip())).lower()}")
        return 0 if result.content.strip() else 1
    except (ValueError, AzureError, APIConnectionError, APITimeoutError, APIStatusError) as error:
        print(f"Smoke test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())