# get_waste_collections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 28th tool, `get_waste_collections`, returning upcoming municipal waste-collection dates for any Luxembourg commune from the official calendar on data.public.lu.

**Architecture:** One provider method on `LuxembourgData` following the established "resolve dataset resource → fetch with host allowlist → cache parsed payload → filter in memory" pattern (closest models: `search_parliamentary_questions`, `get_housing_prices`). Registration, tests, catalog page, and README follow the repo's five-place checklist for new tools.

**Tech Stack:** Python stdlib only (`csv`, `datetime`, `unicodedata`). Zero runtime dependencies is a hard constraint.

**Spec:** `docs/superpowers/specs/2026-07-31-waste-collections-design.md`. Two deliberate deviations toward repo conventions: (1) the resource URL is resolved per call via `_dataset_resource()` and only the CSV payload is cached (keyed by URL, 1 h TTL); (2) the `dataset` result key is the dataset page URL (`dataset.get("page")`), not the slug, matching every other data.public.lu tool.

## Global Constraints

- `dependencies = []` in pyproject.toml — stdlib only, no new runtime dependencies.
- Every tool result must include a `"source"` key with the upstream URL.
- URLs from data.public.lu dataset metadata must be fetched with `allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS`.
- `TypeError`/`ValueError` → "Invalid arguments" tool error; `UpstreamError` → upstream-message tool error. Never JSON-RPC protocol errors.
- Test command: `PYTHONPATH=src python -m unittest <target> -v` (run from repo root).
- Git commits: NO `Co-Authored-By` or AI-attribution trailers, ever.
- Tool count changes 27 → 28 everywhere; the "18 official systems" figure does NOT change (Administration de l'environnement is already counted via `get_air_quality`).

---

### Task 1: Provider method `get_waste_collections`

**Files:**
- Modify: `src/luxembourg_mcp/providers.py` (constant near line 48, method after `get_ev_charging` at end of class)
- Test: `tests/test_server.py` (new `WasteProviderTests` class after `NewProviderTests`)

**Interfaces:**
- Consumes: existing helpers `self._dataset_resource(slug, *, format)`, `self._cached(key, ttl, loader)`, `self._decode_csv(payload, delimiter)`, module function `_fold(text)`, constant `DATA_PUBLIC_RESOURCE_HOSTS`.
- Produces: `LuxembourgData.get_waste_collections(commune: str, street: str | None = None, waste_type: str | None = None, limit: int = 20) -> dict` returning keys `commune, count, total_matches, collections, source, dataset`. Each collection item: `{"date": "YYYY-MM-DD", "type": str, "locality": str | None, "street": str | None}`. Task 2 registers this exact callable.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`, after the `NewProviderTests` class (module-level fixture helper first, near the existing `_xlsx_fixture`):

```python
def _waste_fixture():
    dataset = {
        "id": "w", "slug": "waste-calendars", "page": "https://data.public.lu/en/datasets/waste-calendars/",
        "resources": [{"id": "1", "title": "calendrierdechet.csv", "format": "csv",
                       "url": "https://download.data.public.lu/resources/waste/calendrierdechet.csv"}],
    }
    csv_payload = (
        '\ufeff"Date";"Type de collecte";"Commune";"Localité";"Rue"\n'
        '"01/01/2020";"Verre";"Bech";"Altrier";"Am Reimergaard"\n'
        '"03/03/2099";"Biodéchets";"Bech";;"Toutes les rues"\n'
        '"02/02/2099";"Verre";"Bech";"Altrier";"Am Reimergaard"\n'
        '"05/05/2099";"Verre";"Ettelbrück";;"Toutes les rues"\n'
    ).encode("utf-8")
    return dataset, csv_payload


class WasteProviderTests(unittest.TestCase):
    def test_waste_collections_sorts_iso_dates_and_drops_past_rows(self):
        dataset, payload = _waste_fixture()
        result = LuxembourgData(FakeHttp([dataset, payload])).get_waste_collections("Bech")
        self.assertEqual([item["date"] for item in result["collections"]], ["2099-02-02", "2099-03-03"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["commune"], "Bech")
        self.assertTrue(result["source"].startswith("https://download.data.public.lu/"))
        self.assertEqual(result["dataset"], "https://data.public.lu/en/datasets/waste-calendars/")

    def test_waste_collections_matches_commune_without_accents(self):
        dataset, payload = _waste_fixture()
        result = LuxembourgData(FakeHttp([dataset, payload])).get_waste_collections("ettelbruck")
        self.assertEqual(result["commune"], "Ettelbrück")
        self.assertEqual(result["count"], 1)

    def test_waste_collections_street_filter_keeps_commune_wide_rows(self):
        dataset, payload = _waste_fixture()
        result = LuxembourgData(FakeHttp([dataset, payload])).get_waste_collections("Bech", street="reimergaard")
        self.assertEqual(
            [(item["date"], item["street"]) for item in result["collections"]],
            [("2099-02-02", "Am Reimergaard"), ("2099-03-03", "Toutes les rues")],
        )

    def test_waste_collections_type_filter(self):
        dataset, payload = _waste_fixture()
        result = LuxembourgData(FakeHttp([dataset, payload])).get_waste_collections("Bech", waste_type="verre")
        self.assertEqual([item["type"] for item in result["collections"]], ["Verre"])

    def test_waste_collections_unknown_commune_lists_valid_names(self):
        dataset, payload = _waste_fixture()
        with self.assertRaises(ValueError) as caught:
            LuxembourgData(FakeHttp([dataset, payload])).get_waste_collections("Atlantis")
        self.assertIn("Bech", str(caught.exception))
        self.assertIn("Ettelbrück", str(caught.exception))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python -m unittest tests.test_server.WasteProviderTests -v`
Expected: all 5 FAIL/ERROR with `AttributeError: 'LuxembourgData' object has no attribute 'get_waste_collections'`

- [ ] **Step 3: Implement the provider method**

In `src/luxembourg_mcp/providers.py`, add the constant after `CHARGY_SLUG` (line 48):

```python
WASTE_SLUG = "waste-municipal-waste-collection-calendars-dechets-calendriers-municipaux-de-collecte-des-dechets"
```

Add the method at the end of the `LuxembourgData` class, after `get_ev_charging`:

```python
    def get_waste_collections(self, commune: str, street: str | None = None,
                              waste_type: str | None = None, limit: int = 20) -> dict:
        if not commune.strip():
            raise ValueError("commune must not be empty")
        limit = min(max(limit, 1), 100)
        dataset, resource = self._dataset_resource(WASTE_SLUG, format="csv")
        url = resource["url"]

        def load() -> list[dict[str, str]]:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            return self._decode_csv(payload, delimiter=";")

        rows = self._cached(f"waste:{url}", 3600, load)
        communes = sorted({row["Commune"] for row in rows if row.get("Commune")})
        if not communes:
            raise UpstreamError("Waste calendar CSV had an unexpected layout")
        needle = _fold(commune)
        canonical = next((name for name in communes if _fold(name) == needle), None)
        if canonical is None:
            candidates = [name for name in communes if needle in _fold(name)]
            if len(candidates) == 1:
                canonical = candidates[0]
            elif candidates:
                raise ValueError(f"commune is ambiguous; matches: {', '.join(candidates)}")
            else:
                raise ValueError(f"unknown commune; valid names: {', '.join(communes)}")
        today = datetime.now().date().isoformat()
        street_needle = _fold(street) if street else None
        type_needle = _fold(waste_type) if waste_type else None
        matches = []
        for row in rows:
            if row.get("Commune") != canonical:
                continue
            try:
                iso = datetime.strptime(row.get("Date") or "", "%d/%m/%Y").date().isoformat()
            except ValueError:
                continue
            if iso < today:
                continue
            rue = row.get("Rue") or ""
            # "Toutes les rues" rows apply commune-wide, so they pass any street filter.
            if street_needle and "toutes les rues" not in _fold(rue) and street_needle not in _fold(rue):
                continue
            if type_needle and type_needle not in _fold(row.get("Type de collecte") or ""):
                continue
            matches.append({"date": iso, "type": row.get("Type de collecte"),
                            "locality": row.get("Localité") or None, "street": rue or None})
        matches.sort(key=lambda item: item["date"])
        return {"commune": canonical, "count": len(matches[:limit]), "total_matches": len(matches),
                "collections": matches[:limit], "source": url, "dataset": dataset.get("page")}
```

Notes for the implementer: `datetime` is already imported (`from datetime import datetime`); `_fold`, `UpstreamError`, `DATA_PUBLIC_RESOURCE_HOSTS` already exist in the module. Do not add imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m unittest tests.test_server.WasteProviderTests -v`
Expected: 5 PASS

- [ ] **Step 5: Run the whole offline suite to check nothing regressed**

Run: `PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: only pre-existing failures related to tool count, if any (there should be none yet — the tool is not registered).

- [ ] **Step 6: Commit**

```bash
git add src/luxembourg_mcp/providers.py tests/test_server.py
git commit -m "Add waste-collection calendar provider method"
```

---

### Task 2: Register the tool and update count assertions

**Files:**
- Modify: `src/luxembourg_mcp/server.py` (tool list ~line 172, `instructions` string ~line 209)
- Modify: `tests/test_all_tools.py` (TOOL_CASES + count)
- Modify: `tests/test_server.py` (`test_lists_twenty_seven_tools` ~line 238)

**Interfaces:**
- Consumes: `source.get_waste_collections` from Task 1 (exact signature above).
- Produces: registered tool name `"get_waste_collections"` with schema requiring `commune`; Tasks 3–4 rely on the registered name and the 28-tool count.

- [ ] **Step 1: Update the contract tests first (they will fail)**

In `tests/test_all_tools.py`: add to `TOOL_CASES` after the `get_ev_charging` entry:

```python
    "get_waste_collections": {"commune": "Bech", "waste_type": "verre", "limit": 5},
```

and change the count assertion:

```python
        self.assertEqual(len(TOOL_CASES), 28)
```

In `tests/test_server.py`, rename and update:

```python
    def test_lists_twenty_eight_tools(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(response["result"]["tools"]), 28)
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src python -m unittest tests.test_all_tools tests.test_server.ProtocolTests -v`
Expected: FAIL — set mismatch (`get_waste_collections` missing from registry) and 27 != 28.

- [ ] **Step 3: Register the tool**

In `src/luxembourg_mcp/server.py`, append after the `get_ev_charging` `Tool(...)` line (~line 172), same single-line style as its neighbours:

```python
                Tool("get_waste_collections", "Get upcoming municipal waste-collection dates for a Luxembourg commune.", _object_schema({"commune": {"type": "string"}, "street": {"type": "string", "description": "Optional street-name filter; commune-wide rows always match"}, "waste_type": {"type": "string", "description": "Optional collection-type filter such as verre, papier, biodechets"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["commune"]), source.get_waste_collections),
```

In the same file, update the `instructions` string (~line 209): `"...through 27 tools..."` → `"...through 28 tools..."`.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src python -m unittest tests.test_all_tools tests.test_server -v`
Expected: all PASS except `test_catalog_is_packaged` (still asserts 27 tool-cards — fixed in Task 3).

- [ ] **Step 5: Commit**

```bash
git add src/luxembourg_mcp/server.py tests/test_all_tools.py tests/test_server.py
git commit -m "Register get_waste_collections tool"
```

---

### Task 3: Catalog page card and counts

**Files:**
- Modify: `src/luxembourg_mcp/static/index.html` (lines 9, 26, 283, 294, 352, and the tool grid ending ~line 340)
- Modify: `tests/test_server.py` (`test_catalog_is_packaged` ~line 307)

**Interfaces:**
- Consumes: tool name `get_waste_collections` from Task 2.
- Produces: nothing consumed later; the packaged catalog test locks the card count at 28.

- [ ] **Step 1: Update the catalog test first**

In `tests/test_server.py` `test_catalog_is_packaged`:

```python
        self.assertEqual(page.count('class="tool-card"'), 28)
        self.assertIn("<strong>18</strong> official systems", page)
```

Run: `PYTHONPATH=src python -m unittest tests.test_server.ProtocolTests.test_catalog_is_packaged -v`
Expected: FAIL (27 cards in the page).

- [ ] **Step 2: Add the tool card and bump every count in index.html**

Append after the `get_ev_charging` card (~line 340), matching the one-line card style:

```html
      <article class="tool-card" data-source="environment"><div class="tool-top"><span class="source-name"><b>Environnement</b></span><span class="keyless">Keyless</span></div><h3>get_waste_collections</h3><p>Upcoming municipal waste-collection dates by commune.</p><span class="arguments">commune · street? · waste_type? · limit?</span></article>
```

Then update every "27" tool count in the file — exactly these five spots, leaving "18" untouched:
1. Line 9 `og:description`: `"27 MCP tools over 18 official systems: ..."` → `"28 MCP tools over 18 official systems: ..."`
2. Line 26 JSON-LD `description`: `"...27 tools over 18 public systems..."` → `"...28 tools over 18 public systems..."`
3. Line 283 hero stat: `<div><strong>27</strong><span>tools</span></div>` → `<strong>28</strong>`
4. Line 294: `Showing <b id="shown">27</b> of 27 tools` → `Showing <b id="shown">28</b> of 28 tools`
5. Line 352: `discovers all <b>27</b> tools across <strong>18</strong>` → `all <b>28</b> tools`

Verify no stragglers: `grep -n "27" src/luxembourg_mcp/static/index.html` should return no tool-count references.

- [ ] **Step 3: Run to verify it passes**

Run: `PYTHONPATH=src python -m unittest tests.test_server.ProtocolTests.test_catalog_is_packaged -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/luxembourg_mcp/static/index.html tests/test_server.py
git commit -m "Add get_waste_collections to the catalog page"
```

---

### Task 4: README row, live test, and stray-count sweep

**Files:**
- Modify: `README.md` (lines 11, 13, table after line 58, line 142)
- Modify: `tests/test_live_tools.py` (new test method after `test_get_ev_charging`)

**Interfaces:**
- Consumes: registered tool `get_waste_collections` (Task 2); live-call helper `self.call(name, arguments)` already in `EveryToolLiveTests`.
- Produces: nothing consumed later.

- [ ] **Step 1: Add the live test**

In `tests/test_live_tools.py`, after `test_get_ev_charging`:

```python
    def test_get_waste_collections(self):
        data = self.call("get_waste_collections", {"commune": "Dudelange", "limit": 3})
        self.assertGreaterEqual(data["count"], 1)
        self.assertTrue(all(len(item["date"]) == 10 for item in data["collections"]))
```

- [ ] **Step 2: Update README.md**

1. Line 11: `...into 27 consistent tools...` → `...into 28 consistent tools...`
2. Line 13: `- 27 MCP tools` → `- 28 MCP tools`
3. Add table row after the `get_ev_charging` row (line 58):

```markdown
| `get_waste_collections` | Environment Administration | Upcoming waste-collection dates by commune |
```

4. Line 142: `...the `tools/call` contract for all 27 tools.` → `...all 28 tools.`

- [ ] **Step 3: Sweep for leftover count references**

Run: `grep -rn "27 tools\|27 MCP\|27 consistent" README.md server.json src/ deploy/ docs/ --include="*.md" --include="*.json" --include="*.py" --include="*.html" --include="*.jsonc"`
Expected: no hits (the spec's "27 → 28" applies repo-wide). Fix any stragglers found.

- [ ] **Step 4: Run the full offline suite**

Run: `PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: ALL PASS (live tests auto-skip without `LUXEMBOURG_MCP_LIVE=1`).

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_live_tools.py
git commit -m "Document get_waste_collections and add live coverage"
```

---

### Task 5: Live verification

**Files:** none modified.

**Interfaces:** consumes the finished tool end-to-end against the real upstream.

- [ ] **Step 1: Run the new tool's live test against the real upstream**

Run: `LUXEMBOURG_MCP_LIVE=1 PYTHONPATH=src python -m unittest tests.test_live_tools.EveryToolLiveTests.test_get_waste_collections -v`
Expected: PASS (fetches the real ~9 MB CSV; allow up to ~30 s).

- [ ] **Step 2: Report**

No commit. Report the live result verbatim; if the upstream is temporarily down, say so rather than retrying indefinitely.
