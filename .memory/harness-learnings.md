# Harness Learnings

Workspace-local implementation and verification findings.

## L-001: MCP verification needs explicit public PyPI fallback

- Phase: verify
- Dimension: maintainability
- Scope: services/migration_mcp/requirements.txt, tests/test_migration_mcp.py
- Pattern: The configured package feed at packagefeedproxy.microsoft.io timed out and reported no MCP distributions, while public PyPI resolved and installed the same valid requirement.
- Guidance: If MCP dependency installation reports no matching distribution from the configured proxy, retry with `.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r services\migration_mcp\requirements.txt` before changing version constraints.
- Confidence: low
- Occurrences: 1
- First seen: 2026-08-21
- Last seen: 2026-08-21

## L-002: FastMCP mounted app lifespan belongs to its router

- Phase: implement
- Dimension: architecture
- Scope: services/migration_mcp/app.py
- Pattern: With MCP 1.29, `FastMCP.streamable_http_app()` returns a Starlette application without an `app.lifespan` attribute; its required session-manager lifecycle is exposed as `app.router.lifespan_context`.
- Guidance: When wrapping the FastMCP Streamable HTTP app in a parent Starlette app, pass `mcp_http_app.router.lifespan_context` as the parent `lifespan` and retain the focused TestClient startup test.
- Confidence: low
- Occurrences: 1
- First seen: 2026-08-21
- Last seen: 2026-08-21
