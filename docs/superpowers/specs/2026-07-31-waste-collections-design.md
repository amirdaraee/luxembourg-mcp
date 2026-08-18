# Design: `get_waste_collections` tool

Date: 2026-07-31
Status: approved

## Goal

Add a 28th tool to luxembourg-mcp: upcoming municipal waste-collection dates for any
Luxembourg commune, from the official Administration de l'environnement calendar
published on data.public.lu. Answers questions like "when is the next glass pickup in
Dudelange?".

## Upstream

- Dataset slug: `waste-municipal-waste-collection-calendars-dechets-calendriers-municipaux-de-collecte-des-dechets`
- One CSV resource on `download.data.public.lu` (already in `DATA_PUBLIC_RESOURCE_HOSTS`),
  ~9 MB, ~112k rows, refreshed daily. The resource URL embeds a timestamp, so it must be
  resolved at call time via the catalog API (`get_dataset`), never hardcoded — same
  pattern and security invariant as `get_weather_alerts`.
- CSV shape: semicolon-delimited, UTF-8 with BOM. Columns: `Date` (DD/MM/YYYY),
  `Type de collecte` (19 free-form French labels, e.g. "Biodéchets", "Verre – 40L"),
  `Commune` (88 values), `Localité` (may be empty), `Rue` (may be "Toutes les rues").
- Rolling forward window of roughly three months.

## Tool interface

```
get_waste_collections(
  commune: string (required),
  street?: string,       # substring filter over Rue
  waste_type?: string,   # substring filter over Type de collecte, e.g. "verre"
  limit?: integer = 20   # 1–100
)
```

Schema uses only keywords `validate_schema` already supports (`type`, `minimum`,
`maximum`, `default`, required).

## Provider behaviour (`LuxembourgData.get_waste_collections`)

1. **Fetch + cache**: `_cached("waste_calendar", 3600)` wraps: resolve dataset via
   `get_dataset(WASTE_CALENDAR_SLUG)`, pick the latest CSV resource, download with
   `allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS`, parse with
   `csv.DictReader(..., delimiter=";")` decoding `utf-8-sig`. Cache value = (rows,
   resolved source URL). All filtering runs on the cached in-memory rows. 1 h TTL
   matches the GTFS zip precedent.
2. **Commune matching**: normalize both sides with casefold + Unicode accent stripping
   (`unicodedata`, stdlib). Exact normalized match wins; otherwise a unique substring
   match. No match → `ValueError` whose message lists all valid commune names
   (self-discovery without a second tool). Ambiguous substring → `ValueError` listing
   the candidates.
3. **Filters**: `street` and `waste_type` are normalized substring filters. Rows with
   `Rue == "Toutes les rues"` always pass the street filter (they apply commune-wide).
4. **Shaping**: convert dates to ISO `YYYY-MM-DD`, drop rows before today, sort
   ascending by date, cap at `limit`.

Result:

```json
{
  "commune": "<canonical name>",
  "collections": [{"date", "type", "locality", "street"}, ...],
  "total_matches": N,
  "source": "<resolved CSV URL>",
  "dataset": "<slug>"
}
```

`source` is mandatory per the repo convention; `dataset` follows the convention used by
other data.public.lu-backed tools.

## Errors

- Missing/invalid arguments, unknown or ambiguous commune → `ValueError` → "Invalid
  arguments" tool error.
- Missing CSV resource, download failure, malformed CSV → `UpstreamError` → upstream
  message as tool error.
- Both surface as tool errors (`isError: true`), never JSON-RPC protocol errors.

## Five-place checklist

1. Provider method on `LuxembourgData` (providers.py) + `WASTE_CALENDAR_SLUG` constant.
2. `Tool("get_waste_collections", ...)` registration in server.py; tool count in
   `instructions` 27 → 28.
3. `TOOL_CASES` entries in `tests/test_all_tools.py` (offline fixture CSV through the
   fake HTTP client; also cover unknown-commune error, diacritic-insensitive matching,
   "Toutes les rues" street behaviour, date filtering/ISO conversion) and
   `tests/test_live_tools.py`.
4. `tool-card` in `src/luxembourg_mcp/static/index.html`; card count and "N official
   systems" figure updated.
5. README tool table row.

## Out of scope

- No second tool (`list_waste_communes`) — the unknown-commune error message covers
  discovery.
- No enum for waste types — labels are free-form and change; substring filter instead.
- No version bump beyond the normal release flow.
