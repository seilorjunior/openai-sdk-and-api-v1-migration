#!/usr/bin/env python3
"""Test Azure OpenAI user security context directly or through APIM.

------------------------------------------------
This test uses the official OpenAI Python client and the Azure OpenAI v1 route.
The ``user_security_context`` extension is added to the chat-completions request
through ``extra_body``. The v1 URL does not guarantee that every deployment
supports every Azure-specific extension. Microsoft documents this context for
direct Azure OpenAI deployments, but not for models consumed through the Azure
AI Model Inference API. Therefore, ``unrecognized_request_argument`` means that
the selected deployment/API rejected this capability; it does not mean that the
request used a legacy API or that the OpenAI client serialized it incorrectly.

Este teste usa o cliente Python oficial da OpenAI e a rota v1 do Azure OpenAI.
A extensao ``user_security_context`` e adicionada ao request de chat completions
por ``extra_body``. A URL v1 nao garante que todos os deployments aceitem todas
as extensoes especificas do Azure. A Microsoft documenta esse contexto para
deployments Azure OpenAI diretos, mas nao para modelos consumidos pela Azure AI
Model Inference API. Portanto, ``unrecognized_request_argument`` significa que
o deployment/API selecionado rejeitou essa capacidade; nao significa que o
request usou uma API legada ou que o cliente OpenAI o serializou incorretamente.

By default, output is sanitized. ``--print-full-exchange`` prints the complete
JSON request and response bodies only when combined with
``--acknowledge-sensitive-output``. Authentication headers, API keys, and access
tokens are never printed.

For direct lab runs, missing Azure endpoint/deployment values are loaded from
the current ``azd`` environment. Missing user-context values use synthetic test
defaults and can be overridden with environment variables for customer tests.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from smoke_test import build_client, require_env

CUSTOMER_EXPLANATION = {
    "en": (
        "The call uses the official OpenAI Python client and the Azure OpenAI v1 route. "
        "The v1 route is correct, but support for Azure-specific request extensions still depends on the selected "
        "deployment and backend. Microsoft documents user_security_context for direct Azure OpenAI deployments and "
        "does not support it for models consumed through the Azure AI Model Inference API. An "
        "unrecognized_request_argument response therefore indicates an unsupported deployment/API capability, not "
        "use of a legacy API or incorrect serialization by the client."
    ),
    "pt_BR": (
        "A chamada usa o cliente Python oficial da OpenAI e a rota v1 do Azure OpenAI. A rota v1 esta correta, mas "
        "o suporte a extensoes especificas do Azure ainda depende do deployment e do backend selecionados. A "
        "Microsoft documenta user_security_context para deployments Azure OpenAI diretos e nao oferece suporte para "
        "modelos consumidos pela Azure AI Model Inference API. Portanto, uma resposta "
        "unrecognized_request_argument indica que o deployment/API nao suporta essa capacidade, e nao que foi usada "
        "uma API legada ou que o cliente serializou o request incorretamente."
    ),
}

LAB_SECURITY_CONTEXT_DEFAULTS = {
    "OPENAI_SECURITY_APPLICATION_NAME": "openai-migration-lab",
    "OPENAI_SECURITY_END_USER_ID": "11111111-1111-1111-1111-111111111111",
    "OPENAI_SECURITY_END_USER_TENANT_ID": "22222222-2222-2222-2222-222222222222",
    "OPENAI_SECURITY_SOURCE_IP": "192.0.2.10",
}


def get_azd_value(name: str) -> str:
    result = subprocess.run(
        ["azd", "env", "get-value", name],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ValueError(f"Unable to read {name} from the current azd environment.")
    value = result.stdout.strip().strip('"')
    if not value:
        raise ValueError(f"The current azd environment does not define {name}.")
    return value


def configure_lab_environment(target: str) -> None:
    for name, value in LAB_SECURITY_CONTEXT_DEFAULTS.items():
        os.environ.setdefault(name, value)

    if target == "direct":
        for name in ("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_DEPLOYMENT"):
            if not os.getenv(name):
                os.environ[name] = get_azd_value(name)


def build_user_security_context() -> dict[str, str]:
    context = {
        "application_name": require_env("OPENAI_SECURITY_APPLICATION_NAME"),
        "end_user_id": require_env("OPENAI_SECURITY_END_USER_ID"),
        "source_ip": require_env("OPENAI_SECURITY_SOURCE_IP"),
    }
    tenant_id = os.getenv("OPENAI_SECURITY_END_USER_TENANT_ID")
    if tenant_id:
        context["end_user_tenant_id"] = tenant_id
    return context


def build_request_body(prompt: str) -> dict[str, Any]:
    return {
        "model": require_env("AZURE_OPENAI_DEPLOYMENT"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "temperature": 0,
        "extra_body": {"user_security_context": build_user_security_context()},
    }


def submit_request(client: OpenAI, request_body: dict[str, Any]) -> tuple[dict[str, bool], Any]:
    response = client.chat.completions.create(**cast(Any, request_body))
    result = {
        "output_nonempty": bool(response.choices[0].message.content),
        "security_context_submitted": True,
    }
    return result, response


def run_test(client: OpenAI, prompt: str) -> dict[str, bool]:
    result, _ = submit_request(client, build_request_body(prompt))
    return result


def is_unsupported_user_security_context(error: APIStatusError) -> bool:
    body = error.body
    if not isinstance(body, dict):
        return False

    code = body.get("code")
    message = body.get("message")
    return code == "unrecognized_request_argument" and isinstance(message, str) and "user_security_context" in message


def serialize_response(response: Any) -> Any:
    model_dump = getattr(response, "model_dump", None)
    if not callable(model_dump):
        raise TypeError("The SDK response does not support JSON serialization through model_dump().")
    return model_dump(mode="json")


def serialize_request_body(request_args: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in request_args.items() if key != "extra_body"}
    extra_body = request_args.get("extra_body")
    if isinstance(extra_body, dict):
        body.update(extra_body)
    return body


def full_exchange(request_body: dict[str, Any], response_body: Any) -> dict[str, Any]:
    return {
        "notice": (
            "Sensitive output explicitly enabled. Request and response JSON bodies are included; authentication "
            "headers, API keys, and access tokens are excluded."
        ),
        "request_body": request_body,
        "response_body": response_body,
    }


def save_full_exchange(path: Path, exchange: dict[str, Any]) -> None:
    path.write_text(json.dumps(exchange, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def include_or_save_full_exchange(
    args: argparse.Namespace,
    output: dict[str, Any],
    request_body: dict[str, Any],
    response_body: Any,
) -> None:
    exchange = full_exchange(serialize_request_body(request_body), response_body)
    if args.print_full_exchange:
        output["full_exchange"] = exchange
    if args.save_full_exchange is not None:
        try:
            save_full_exchange(args.save_full_exchange, exchange)
            output["full_exchange_file"] = str(args.save_full_exchange)
        except OSError as error:
            output["full_exchange_file_error"] = str(error)


def validate_sensitive_output_args(args: argparse.Namespace) -> None:
    if (args.print_full_exchange or args.save_full_exchange is not None) and not args.acknowledge_sensitive_output:
        raise ValueError(
            "Full exchange output contains user identity and IP data; also pass "
            "--acknowledge-sensitive-output to confirm."
        )


def api_error_body(error: APIStatusError) -> Any:
    return {
        "status_code": error.status_code,
        "body": error.body,
    }


def passed_output(target: str, result: dict[str, bool]) -> dict[str, Any]:
    return {
        "target": target,
        "status": "passed",
        "customer_explanation": CUSTOMER_EXPLANATION,
        **result,
    }


def unsupported_output(target: str) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "error_code": "unrecognized_request_argument",
        "reason": "deployment_or_api_does_not_support_user_security_context",
        "target": target,
        "customer_explanation": CUSTOMER_EXPLANATION,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("direct", "apim"), required=True)
    parser.add_argument("--prompt", default="Return only: user security context accepted")
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


def main(args: argparse.Namespace | None = None) -> int:
    args = args or parse_args()
    request_body: dict[str, Any] | None = None
    try:
        validate_sensitive_output_args(args)
        configure_lab_environment(args.target)
        request_body = build_request_body(args.prompt)
        result, response = submit_request(build_client(args.target), request_body)
        output = passed_output(args.target, result)
        if args.print_full_exchange or args.save_full_exchange is not None:
            include_or_save_full_exchange(args, output, request_body, serialize_response(response))
        print(json.dumps(output, sort_keys=True))
        return 0 if result["output_nonempty"] else 1
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    except APIStatusError as error:
        if is_unsupported_user_security_context(error):
            output = unsupported_output(args.target)
            if (args.print_full_exchange or args.save_full_exchange is not None) and request_body is not None:
                include_or_save_full_exchange(args, output, request_body, api_error_body(error))
            print(json.dumps(output, sort_keys=True))
            return 2

        api_error_output: dict[str, Any] = {
            "status": "failed",
            "error_type": type(error).__name__,
            "status_code": error.status_code,
        }
        if (args.print_full_exchange or args.save_full_exchange is not None) and request_body is not None:
            include_or_save_full_exchange(args, api_error_output, request_body, api_error_body(error))
        print(json.dumps(api_error_output, sort_keys=True))
        return 1
    except (APIConnectionError, APITimeoutError) as error:
        transport_error_output: dict[str, Any] = {
            "status": "failed",
            "error_type": type(error).__name__,
            "status_code": getattr(error, "status_code", None),
        }
        print(json.dumps(transport_error_output, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())