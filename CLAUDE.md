# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e .                                            # install (editable)

PYTHONPATH=src python -m unittest discover -s tests -v     # offline test suite (deterministic, no network)
PYTHONPATH=src python -m unittest tests.test_server.ProtocolTests.test_initialize   # single test
LUXEMBOURG_MCP_LIVE=1 PYTHONPATH=src python -m unittest tests.test_live_tools -v    # opt-in E2E against real upstreams

luxembourg-mcp                                              # stdio transport (default)
luxembourg-mcp --transport http --port 8000                 # HTTP: /mcp endpoint, / catalog, /health
```

`LUXEMBOURG_MCP_RATE_LIMIT` sets the HTTP per-IP requests/minute limit (0 disables).

## Hard constraint: zero dependencies

`dependencies = []` in pyproject.toml is deliberate. The MCP protocol (JSON-RPC), HTTP client, XML/CSV/zip parsing, and JSON Schema validation are all hand-implemented on the stdlib. Do not add runtime dependencies; extend the existing minimal implementations instead (e.g. `validate_schema` in server.py supports only the schema keywords the tools actually declare).

## Architecture

Three layers, strictly ordered:

- `src/luxembourg_mcp/http.py` — `HttpClient` wraps urllib; every network failure becomes `UpstreamError`. Enforces a 25 MB response cap and, when `allowed_hosts` is passed, exact-hostname HTTPS allowlisting including on redirects (`_SafeRedirectHandler`).
- `src/luxembourg_mcp/providers.py` — `LuxembourgData`, one method per tool ("fetch → parse → shape"). Upstream base URLs are hardcoded constants. TTL cache via `_cached()` for expensive fetches (STATEC catalog and GTFS zip: 1 h; air quality: 10 min). `HttpClient` is constructor-injected, which is what lets tests run offline with a fake.
- `src/luxembourg_mcp/server.py` — `McpServer`: the tool registry (name → `Tool` dataclass with JSON schema), JSON-RPC dispatch in `handle()`, schema validation, and both transports (stdio loop; stateless `ThreadingHTTPServer` with body-size cap, `RateLimiter`, and localhost-Origin check).

Error contract in `tools/call`: `TypeError`/`ValueError` → "Invalid arguments", `UpstreamError` → upstream message — both returned as *tool errors* (`isError: true` in a successful JSON-RPC result, visible to the calling model), never as JSON-RPC protocol errors.

Every tool result must include a `"source"` key with the upstream URL (several also include `"dataset"`); tests and the README rely on this convention.

## Adding or changing a tool touches five places

The contract tests enforce set-equality between registered tools and test cases, and the catalog test asserts the exact tool count, so a new tool requires all of:

1. Provider method on `LuxembourgData` (providers.py)
2. `Tool(...)` registration with input schema (server.py `McpServer.__init__`)
3. Entry in `TOOL_CASES` in **both** `tests/test_all_tools.py` and `tests/test_live_tools.py`
4. A `tool-card` in `src/luxembourg_mcp/static/index.html` (test asserts card count and the "N official systems" figure)
5. The tool table in README.md and the tool count mentioned in server.py `instructions`

## Security invariants (tested in tests/test_security.py — do not regress)

- Validate URLs/origins by parsing and comparing the exact hostname, never `startswith`/substring (past bug: `http://localhost.evil.example` bypassed the Origin check).
- URLs taken from data.public.lu dataset metadata (not hardcoded) must be fetched with `allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS`.
- Size limits are enforced on observed bytes, not declared headers: HTTP request body (1 MB), upstream responses (25 MB), zip members via `_read_bounded_zip_member` (10 MB + compression-ratio check).
- New user inputs that reach URLs need a regex allowlist or `quote()` (see `get_statistics`, `get_cfl_parking`).

## Conventions

- Version string lives in six places and must stay in sync: `pyproject.toml`, `__init__.__version__`, the User-Agent in http.py, `serverInfo` in server.py, `server.json` (two fields), and `RELEASE` in `deploy/cloudflare/wrangler.jsonc`.
- The hosted endpoint (deploy/cloudflare) routes to a Durable Object named `main-${RELEASE}`: an existing DO keeps its originally provisioned container image across rolling deploys, so bumping `RELEASE` is what actually ships new server code to mcp.luxembourg-mcp.com. Deploy with `npx wrangler deploy` from deploy/cloudflare (Docker must be running); verify with an MCP `initialize` against the live endpoint.
- Keyless only: no upstream that requires an API key, account, or scraping.
- Do not add `Co-Authored-By` / AI-attribution trailers to git commits.
