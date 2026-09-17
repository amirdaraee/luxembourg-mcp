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

`LUXEMBOURG_MCP_RATE_LIMIT` sets the HTTP per-IP requests/minute limit (0 disables; IPv6 bucketed by /64). `LUXEMBOURG_MCP_MAX_CONNECTIONS` caps concurrent HTTP connections (default 32).

## Hard constraint: zero dependencies

`dependencies = []` in pyproject.toml is deliberate. The MCP protocol (JSON-RPC), HTTP client, XML/CSV/zip parsing, and JSON Schema validation are all hand-implemented on the stdlib. Do not add runtime dependencies; extend the existing minimal implementations instead (e.g. `validate_schema` in server.py supports only the schema keywords the tools actually declare).

## Architecture

Three layers, strictly ordered:

- `src/luxembourg_mcp/http.py` — `HttpClient` wraps urllib; every network failure becomes `UpstreamError`. Enforces a 25 MB response cap and exact-hostname HTTPS allowlisting including on redirects (`_SafeRedirectHandler`): `allowed_hosts` when passed, otherwise the request URL's own host only (so a hardcoded upstream that starts redirecting cross-host fails loudly in the weekly live tests — update the constant rather than widening the policy).
- `src/luxembourg_mcp/providers.py` — `LuxembourgData`, one method per tool ("fetch → parse → shape"). Upstream base URLs are hardcoded constants. TTL cache via `_cached()` for expensive fetches (STATEC catalog, GTFS zip, day-ahead prices and commune registers: 1 h; air quality and LU-Alert metadata: 10 min; FLEX and Vël'OK: 2 min). `_latest_resource()` picks the newest file in datasets that publish one per day/quarter. `HttpClient` is constructor-injected, which is what lets tests run offline with a fake.
- `src/luxembourg_mcp/server.py` — `McpServer`: the tool registry (name → `Tool` dataclass with JSON schema), JSON-RPC dispatch (`dispatch()` → HTTP status + body; `handle()` wraps it for stdio/tests), schema validation, and both transports (stdio loop; stateless `ThreadingHTTPServer` with body-size cap, `RateLimiter`, and localhost-Origin check).

Dual-era protocol: a request whose `params._meta` carries `io.modelcontextprotocol/protocolVersion` is served statelessly per MCP 2026-07-28 (`_dispatch_modern`: `server/discover`, `tools/list`/`tools/call` with `resultType`, `ttlMs`/`cacheScope`, `_meta` serverInfo; over HTTP the `MCP-Protocol-Version`/`Mcp-Method`/`Mcp-Name` headers must match the body or it's `400` + `-32020`, unsupported versions `400` + `-32022`, unknown methods `404`). Anything else takes the legacy `initialize`-handshake path (`_dispatch_legacy`, 2025-11-25 and earlier), which must stay byte-compatible — `initialize` never negotiates a modern version. Real clients in `auto` mode probe `server/discover` first and fall back to `initialize` on any non-modern error, so a broken modern path silently degrades rather than failing; tests/test_modern_protocol.py covers both eras.

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
- New user inputs that reach URLs need a regex allowlist or `quote()` (see `get_statistics`, `get_cfl_parking`), plus `_require_path_segments()` when `.` is allowed (`quote` leaves `..` intact).
- Parse upstream XML with `_parse_xml()`, never `ElementTree.fromstring` directly: it rejects DTD entity declarations (expat's amplification limit only applies past 8 MiB of input).
- HTTP transport resource bounds: socket timeout (`REQUEST_TIMEOUT_SECONDS`), connection cap (`_BoundedThreadingHTTPServer`), and single-flight `_cached()` so concurrent cold requests trigger one upstream download.
- Malformed input never escapes as an exception: `json.loads` failures catch `(ValueError, RecursionError)`, and request-derived values reaching stderr are control-character escaped.

## Supply chain

- Workflow actions are pinned to commit SHAs (`# vX.Y.Z` comment), the Dockerfile base to a digest, and `deploy/cloudflare/package-lock.json` is committed; `.github/dependabot.yml` bumps all three weekly. Keep new pins in the same form.

## Conventions

- Version string lives in six places and must stay in sync: `pyproject.toml`, `__init__.__version__`, the User-Agent in http.py, `SERVER_INFO` in server.py, `server.json` (two fields), and `RELEASE` in `deploy/cloudflare/wrangler.jsonc` (which may carry a `-N` re-provisioning suffix, see below).
- The hosted endpoint (deploy/cloudflare) routes to a Durable Object named `main-${RELEASE}`: an existing DO keeps its originally provisioned container image across rolling deploys, so bumping `RELEASE` is what actually ships new server code to mcp.luxembourg-mcp.com. Deploy with `npx wrangler deploy` from deploy/cloudflare (Docker must be running); verify with an MCP `initialize` against the live endpoint.
- Deploy race: `wrangler deploy` switches the Worker (and the DO name) instantly, but the container image rollout finishes ~1–2 min later, and any request in that window — public traffic arrives every few seconds — provisions the new DO on the *old* image, permanently. Ship server changes in two deploys: (1) deploy the new image with `RELEASE` still at the previous value, wait until `npx wrangler containers info <app-id> --json` shows `active_rollout_id: null`; (2) set `RELEASE` to the new version and deploy again (Worker-only, "No changes" for containers). Verify with `initialize` → `serverInfo.version`. If a DO still got the old image, re-provision with a `-N` suffix on `RELEASE` (e.g. `0.5.2-1`).
- Keyless only: no upstream that requires an API key, account, or scraping.
- Upstream drift is expected: the weekly live tests are what catch it (e.g. the geocoder moving to apiv4, and inondations.lu dropping its export in favour of a transposed CSV on inondations.public.lu). Fix the constant and the parser rather than loosening a security check.
- Do not add `Co-Authored-By` / AI-attribution trailers to git commits.
