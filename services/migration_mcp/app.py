"""Read-only MCP tools for assessing an Azure OpenAI API v1 migration."""

from __future__ import annotations

import os
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from migration_scan import RULES, json_report, scan_text
from retirement_report import evaluate

MAX_SOURCE_FILES = 25
MAX_SOURCE_BYTES = 64 * 1024
MAX_TOTAL_SOURCE_BYTES = 256 * 1024
BACKEND_KEY_HEADER = b"x-mcp-backend-key"


class SourceDocument(BaseModel):
    """A virtual source file supplied by an MCP caller."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=240)
    source: str


def list_rules() -> list[dict[str, str]]:
    return [{"rule_id": rule_id, "message": message} for rule_id, message, _ in RULES]


def scan_sources(sources: list[SourceDocument]) -> dict[str, object]:
    if not sources:
        raise ValueError("At least one source document is required.")
    if len(sources) > MAX_SOURCE_FILES:
        raise ValueError(f"At most {MAX_SOURCE_FILES} source documents are allowed.")

    total_bytes = 0
    findings = []
    seen_paths: set[str] = set()
    for document in sources:
        if document.path in seen_paths:
            raise ValueError(f"Duplicate virtual path: {document.path}")
        seen_paths.add(document.path)

        source_bytes = len(document.source.encode("utf-8"))
        if source_bytes > MAX_SOURCE_BYTES:
            raise ValueError(f"Source document exceeds {MAX_SOURCE_BYTES} UTF-8 bytes: {document.path}")
        total_bytes += source_bytes
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError(f"Combined source input exceeds {MAX_TOTAL_SOURCE_BYTES} UTF-8 bytes.")
        findings.extend(scan_text(document.source, document.path))

    findings.sort(key=lambda item: (item.path, item.line, item.column, item.rule_id))
    report = json_report(findings)
    report["source_count"] = len(sources)
    report["source_bytes"] = total_bytes
    return report


def allowed_mcp_hosts() -> list[str]:
    hosts = ["127.0.0.1:*", "localhost:*", "testserver"]
    configured_hosts = [
        os.getenv("WEBSITE_HOSTNAME", ""),
        *os.getenv("MCP_ALLOWED_HOSTS", "").split(","),
    ]
    hosts.extend(host.strip() for host in configured_hosts if host.strip())
    return list(dict.fromkeys(hosts))


mcp = FastMCP(
    "OpenAI Migration Toolkit",
    instructions="Read-only tools for identifying legacy Azure OpenAI usage and evaluating retirement evidence.",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=allowed_mcp_hosts()),
)


@mcp.tool()
def list_migration_rules() -> list[dict[str, str]]:
    """List the stable legacy Azure OpenAI migration scan rules."""
    return list_rules()


@mcp.tool()
def scan_migration_sources(sources: list[SourceDocument]) -> dict[str, object]:
    """Scan bounded caller-supplied source text without reading server files."""
    return scan_sources(sources)


@mcp.tool()
def evaluate_retirement_readiness(
    rows: list[dict[str, Any]],
    legacy_p95_baseline_ms: float | None,
    min_success_rate: float = 0.995,
    max_latency_increase: float = 0.10,
    rollback_rehearsed: bool = False,
    parity_passed: bool = False,
    owner_approved: bool = False,
    min_v1_requests: int = 100,
    max_v1_last_request_age_hours: int = 24,
) -> dict[str, Any]:
    """Evaluate supplied Application Insights rows using fail-closed retirement checks."""
    return evaluate(
        rows,
        legacy_p95_baseline_ms=legacy_p95_baseline_ms,
        min_success_rate=min_success_rate,
        max_latency_increase=max_latency_increase,
        rollback_rehearsed=rollback_rehearsed,
        parity_passed=parity_passed,
        owner_approved=owner_approved,
        min_v1_requests=min_v1_requests,
        max_v1_last_request_age_hours=max_v1_last_request_age_hours,
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class BackendKeyMiddleware:
    """Require APIM's shared backend key on the MCP transport only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/mcp"):
            expected_key = os.getenv("MCP_BACKEND_KEY", "")
            supplied_key = dict(scope.get("headers", [])).get(BACKEND_KEY_HEADER, b"").decode("utf-8")
            if not expected_key or not secrets.compare_digest(supplied_key, expected_key):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp_http_app = mcp.streamable_http_app()
starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp_http_app),
    ],
    lifespan=mcp_http_app.router.lifespan_context,
)
app = BackendKeyMiddleware(starlette_app)