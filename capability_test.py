#!/usr/bin/env python3
"""Exercise OpenAI API v1 capabilities directly or through APIM."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from smoke_test import build_client, require_env


class CapabilitySkipped(Exception):
    """A capability is not configured for this environment."""


def optional_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise CapabilitySkipped(f"Set {name} to enable this capability.")
    return value


def timed(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    result = operation()
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return result


def chat(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=80,
        temperature=0,
    )
    return {
        "model": response.model,
        "output_nonempty": bool(response.choices[0].message.content),
        "finish_reason": response.choices[0].finish_reason,
    }


def streaming(client: OpenAI, prompt: str) -> dict[str, Any]:
    chunks = client.chat.completions.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=80,
        temperature=0,
        stream=True,
    )
    chunk_count = 0
    output_length = 0
    for chunk in chunks:
        chunk_count += 1
        output_length += len(chunk.choices[0].delta.content or "") if chunk.choices else 0
    return {"chunk_count": chunk_count, "output_nonempty": output_length > 0}


def responses(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.responses.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        input=prompt,
        max_output_tokens=80,
    )
    return {
        "model": response.model,
        "output_nonempty": bool(response.output_text),
        "status": response.status,
    }


def tools(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_migration_status",
                "description": "Return the migration status for an application.",
                "parameters": {
                    "type": "object",
                    "properties": {"application": {"type": "string"}},
                    "required": ["application"],
                    "additionalProperties": False,
                },
            },
        }],
        tool_choice={"type": "function", "function": {"name": "get_migration_status"}},
        max_tokens=100,
    )
    calls = response.choices[0].message.tool_calls or []
    return {
        "tool_call_count": len(calls),
        "tool_name": calls[0].function.name if calls else None,
        "arguments_valid_json": bool(calls and json.loads(calls[0].function.arguments)),
    }


def structured(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "migration_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "ready": {"type": "boolean"},
                        "summary": {"type": "string"},
                    },
                    "required": ["ready", "summary"],
                    "additionalProperties": False,
                },
            },
        },
        max_tokens=100,
    )
    payload = json.loads(response.choices[0].message.content or "")
    return {
        "schema_valid": isinstance(payload.get("ready"), bool) and isinstance(payload.get("summary"), str),
    }


def embeddings(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.embeddings.create(
        model=optional_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
        input=prompt,
    )
    return {"dimensions": len(response.data[0].embedding), "vector_count": len(response.data)}


def images(client: OpenAI, prompt: str) -> dict[str, Any]:
    response = client.images.generate(
        model=optional_env("AZURE_OPENAI_IMAGE_DEPLOYMENT"),
        prompt=prompt,
        n=1,
    )
    image = response.data[0]
    return {"image_returned": bool(image.url or image.b64_json)}


def audio_transcription(client: OpenAI, _: str) -> dict[str, Any]:
    audio_path = Path(optional_env("OPENAI_AUDIO_FILE"))
    with audio_path.open("rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=optional_env("AZURE_OPENAI_AUDIO_DEPLOYMENT"),
            file=audio_file,
        )
    return {"transcript_nonempty": bool(response.text)}


def safety(client: OpenAI, _: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=require_env("AZURE_OPENAI_DEPLOYMENT"),
        messages=[{"role": "user", "content": optional_env("OPENAI_SAFETY_PROMPT")}],
        max_tokens=80,
        temperature=0,
    )
    choice = response.choices[0]
    filter_results = getattr(choice, "content_filter_results", None)
    return {
        "finish_reason": choice.finish_reason,
        "content_filter_metadata_present": filter_results is not None,
    }


def batch(client: OpenAI, _: str, execute_mutating: bool) -> dict[str, Any]:
    if not execute_mutating:
        raise CapabilitySkipped("Pass --execute-mutating to upload and create a batch job.")
    input_path = Path(optional_env("OPENAI_BATCH_INPUT_FILE"))
    with input_path.open("rb") as input_file:
        uploaded = client.files.create(file=input_file, purpose="batch")
    created = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    return {"batch_id": created.id, "status": created.status}


def fine_tuning(client: OpenAI, _: str, execute_mutating: bool) -> dict[str, Any]:
    if not execute_mutating:
        raise CapabilitySkipped("Pass --execute-mutating to upload and create a fine-tuning job.")
    training_path = Path(optional_env("OPENAI_FINE_TUNING_FILE"))
    with training_path.open("rb") as training_file:
        uploaded = client.files.create(file=training_file, purpose="fine-tune")
    created = client.fine_tuning.jobs.create(
        training_file=uploaded.id,
        model=optional_env("AZURE_OPENAI_FINE_TUNING_MODEL"),
    )
    return {"fine_tuning_job_id": created.id, "status": created.status}


CAPABILITIES: dict[str, Callable[[OpenAI, str], dict[str, Any]]] = {
    "chat": chat,
    "streaming": streaming,
    "responses": responses,
    "tools": tools,
    "structured": structured,
    "embeddings": embeddings,
    "images": images,
    "audio": audio_transcription,
    "safety": safety,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), required=True)
    parser.add_argument("--api-mode", choices=("v1", "legacy"), default="v1")
    parser.add_argument("--capability", choices=(*CAPABILITIES, "batch", "fine-tuning", "all"), default="all")
    parser.add_argument("--prompt", default="Return a concise migration readiness result for application demo.")
    parser.add_argument("--execute-mutating", action="store_true")
    return parser.parse_args()


def run_capability(client: OpenAI, name: str, prompt: str, execute_mutating: bool) -> dict[str, Any]:
    if name == "batch":
        operation = lambda: batch(client, prompt, execute_mutating)
    elif name == "fine-tuning":
        operation = lambda: fine_tuning(client, prompt, execute_mutating)
    else:
        operation = lambda: CAPABILITIES[name](client, prompt)
    return timed(operation)


def main() -> int:
    args = parse_args()
    if args.api_mode != "v1":
        print(json.dumps({"status": "failed", "error": "Advanced capabilities require --api-mode v1."}))
        return 1
    names = list(CAPABILITIES) + ["batch", "fine-tuning"] if args.capability == "all" else [args.capability]
    failed = False
    try:
        client = build_client(args.target)
    except ValueError as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 1

    for name in names:
        try:
            result = run_capability(client, name, args.prompt, args.execute_mutating)
            print(json.dumps({"capability": name, "status": "passed", **result}, sort_keys=True))
        except CapabilitySkipped as error:
            print(json.dumps({"capability": name, "status": "skipped", "reason": str(error)}, sort_keys=True))
        except (ValueError, OSError, json.JSONDecodeError, APIConnectionError, APITimeoutError, APIStatusError) as error:
            failed = True
            status_code = getattr(error, "status_code", None)
            print(json.dumps({"capability": name, "status": "failed", "status_code": status_code, "error_type": type(error).__name__}, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())