# Luxembourg MCP

[![CI](https://github.com/amirdaraee/luxembourg-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/amirdaraee/luxembourg-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/luxembourg-mcp)](https://pypi.org/project/luxembourg-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Keyless Model Context Protocol access to Luxembourg public data.

**Hosted endpoint — no install needed:** point any MCP client at `https://mcp.luxembourg-mcp.com/mcp` (streamable HTTP). Or run it yourself with `uvx luxembourg-mcp`. Website: [luxembourg-mcp.com](https://luxembourg-mcp.com)

Luxembourg MCP turns fragmented public APIs and open datasets into 27 consistent tools that AI agents can call directly. It covers laws, official statistics, mobility, environmental measurements, parliament, accessibility, addresses, geospatial features, and the national open-data catalogue.

- 27 MCP tools
- 18 public data systems
- No API keys or accounts
- No scraping
- Source URL returned with every result
- Stdio and Streamable HTTP transports

## Origin and credit

This project is directly inspired by [Allemannsdata](https://allemannsdata.com/), created by [Thomas Heggelund](https://www.linkedin.com/in/sysfrog/).

Heggelund's original idea was to give public data the same openness suggested by Norway's *allemannsretten*, the right to roam: public information should be straightforward to reach, combine, and use. Allemannsdata removes the repetitive integration work between agents and Norwegian public services by presenting many unrelated data sources through consistent, keyless MCP tools.

Luxembourg MCP applies that idea to the Grand Duchy. It is an independent implementation for Luxembourg, not a fork, official regional edition, or affiliated project. Credit for the original public-data-over-MCP catalogue concept belongs to Thomas Heggelund and Allemannsdata.

## Available tools

| Tool | Public source | Purpose |
| --- | --- | --- |
| `search_datasets` | data.public.lu | Search the national open-data catalogue |
| `get_dataset` | data.public.lu | Inspect metadata and current resources |
| `geocode_address` | Geoportail | Resolve an official address |
| `reverse_geocode` | Geoportail | Find the nearest official address |
| `list_geo_collections` | Geoportail OGC API | Discover queryable geospatial layers |
| `get_geo_features` | Geoportail OGC API | Retrieve bounded GeoJSON features |
| `get_weather_alerts` | MeteoLux | Read current weather warnings |
| `search_legislation` | Legilux | Search legislation and consolidated laws |
| `search_statistics` | STATEC LUSTAT | Discover statistical dataflows |
| `get_statistics` | STATEC LUSTAT | Retrieve labelled SDMX observations |
| `get_city_parking` | Ville de Luxembourg | Read live city parking availability |
| `list_cfl_parking` | CFL | List Park and Ride facilities |
| `get_cfl_parking` | CFL | Read live P+R occupancy and free spaces |
| `get_traffic` | CITA | Read live motorway traffic measurements |
| `get_water_levels` | Water Management Administration | Read current water-station levels |
| `get_air_quality` | Environment Administration | Read national air-quality measurements |
| `search_chamber_bodies` | Chamber of Deputies | Search bodies, memberships, and roles |
| `get_accessibility_figures` | Digital Accessibility Observatory | Read national accessibility totals |
| `get_accessibility_audits` | Digital Accessibility Observatory | List recent public-sector audits |
| `search_transit_stops` | Public Transport Administration | Search nationwide GTFS stops |
| `get_city_mobility` | Ville de Luxembourg Maps | Retrieve mobility locations as GeoJSON |
| `get_weather_observations` | MeteoLux | Live temperature, wind, pressure at Findel |
| `get_public_holidays` | data.public.lu | Legal public holidays in four languages |
| `search_parliamentary_questions` | Chamber of Deputies | Search parliamentary questions and answers |
| `get_housing_prices` | Observatoire de l'Habitat | Advertised housing sale prices by commune |
| `get_election_results` | CTIE | 2023 legislative election results |
| `get_ev_charging` | Chargy | Public EV charging with live availability |

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
luxembourg-mcp --transport http --port 8000
```

Once running:

| Interface | URL |
| --- | --- |
| Tool catalogue | `http://127.0.0.1:8000/` |
| MCP endpoint | `http://127.0.0.1:8000/mcp` |
| Health check | `http://127.0.0.1:8000/health` |

### Stdio client

```json
{
  "mcpServers": {
    "luxembourg": {
      "command": "luxembourg-mcp"
    }
  }
}
```

### Docker

```bash
docker build -t luxembourg-mcp .
docker run --rm -i luxembourg-mcp                                          # stdio (default)
docker run --rm -p 8000:8000 luxembourg-mcp --transport http --host 0.0.0.0  # HTTP
```

## Example questions

After connecting an MCP client, an agent can answer questions such as:

- What weather warnings are active in Luxembourg?
- How many spaces are free at a CFL Park and Ride?
- What is the latest water level at Mersch?
- Find legislation concerning pensions.
- Which STATEC datasets contain population figures?
- What are the latest traffic measurements on the A6?
- Find the official coordinates for an address in Luxembourg City.
- Which bus or tram stops match Hamilius?

## Protocol behavior

The server:

- validates arguments against every advertised tool input schema;
- rejects JSON-RPC batches and other non-object JSON with `-32600 Invalid Request`;
- supports MCP revisions `2025-11-25`, `2025-06-18`, and `2025-03-26`;
- validates the `MCP-Protocol-Version` header for HTTP requests;
- returns tool failures as MCP tool results without terminating the server;
- bounds large upstream responses before placing them in agent context.

The built-in HTTP server is stateless and does not provide an SSE stream. By default it accepts browser origins only from loopback addresses; hosted deployments can extend this with `LUXEMBOURG_MCP_ALLOWED_ORIGINS` (a comma-separated origin list, or `*`), which also enables CORS preflight and response headers for the allowed origins. Public deployments should add TLS, response caching, and observability at a reverse proxy or application gateway.

## Security and deployment

The built-in server includes baseline controls, but it is not a replacement for a production API gateway:

- HTTP request bodies are limited to 1 MiB.
- Upstream responses are limited to 25 MiB.
- Metadata-derived downloads are restricted to trusted `data.public.lu` HTTPS resource hosts, including redirects.
- GTFS ZIP members are checked for decompressed size, encryption, and suspicious compression ratios before extraction.
- HTTP clients are limited to 60 MCP requests per minute by default.
- The Docker image runs as an unprivileged `app` user.

Set `LUXEMBOURG_MCP_RATE_LIMIT` to change the per-minute, per-client limit. A value of `0` disables the built-in limiter. The limiter intentionally uses the direct TCP peer address and does not trust forwarding headers unless `LUXEMBOURG_MCP_CLIENT_IP_HEADER` names one explicitly (for example `CF-Connecting-IP` behind Cloudflare); only set it when a trusted proxy controls that header, since clients could otherwise spoof it to escape the limit.

Tool output is untrusted external data. Dataset descriptions, legislation titles, and other public text may contain misleading or adversarial instructions. MCP clients and agent hosts must treat tool results as data, not system instructions, and should require confirmation or policy checks before allowing powerful sibling tools to act on content returned here.

## Testing

The default suite is deterministic and does not use the network. It covers JSON-RPC behavior, schema validation, provider parsers, catalogue packaging, and the `tools/call` contract for all 27 tools.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The opt-in live suite calls every real upstream service:

```bash
LUXEMBOURG_MCP_LIVE=1 PYTHONPATH=src \
  python -m unittest tests.test_live_tools -v
```

Live tests may fail when a source is unavailable or changes its published data. They also download larger STATEC, air-quality, Chamber, and GTFS resources.

## Current limitations

- The official ATP API for real-time departures and journey planning requires a personal access key. This project exposes keyless static stops from the official GTFS feed instead.
- The cache is bounded but local to one process. Multiple workers do not share cached data.
- Upstream availability, update frequency, schemas, and licensing remain controlled by each data producer.
- Tool coverage is not yet at parity with Allemannsdata. Candidates include parliamentary votes, court decisions, historical weather, EV charging, road works, and more environmental measurements.

## Independence and data ownership

Luxembourg MCP is a community project. It is not affiliated with Thomas Heggelund, Allemannsdata, the Luxembourg government, or any agency whose data it exposes.

The project does not own the upstream data. Each producer's licence and terms continue to apply. Tool results preserve the upstream source URL so agents and users can inspect the authoritative record.

## Licence

The Luxembourg MCP source code is released under the MIT licence. Upstream public data is licensed separately by its respective producer.

---

mcp-name: io.github.amirdaraee/luxembourg-mcp
