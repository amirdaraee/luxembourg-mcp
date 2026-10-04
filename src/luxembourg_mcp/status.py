"""Upstream health for the catalogue's status lights: cheap cached probes, never full downloads."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from . import providers as p
from .http import HttpClient, UpstreamError

REFRESH_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 10
SLOW_SECONDS = 4.0
PROBE_WORKERS = 8
# A round never takes longer than this: a hung upstream must not freeze every light on its last state.
ROUND_DEADLINE_SECONDS = 30
# How long the very first visitor waits for the first round. Request threads count against the HTTP
# connection cap shared with /mcp, so they must not queue behind slow upstreams.
FIRST_WAIT_SECONDS = 5
_RANK = {"ok": 0, "unknown": 1, "degraded": 2, "down": 3}


def _dataset(slug: str) -> tuple[str, str, dict[str, str] | None]:
    # Dataset-backed tools die when the dataset is renamed or withdrawn. API v2 answers 404 for that too, but
    # without inlining every resource: ~3 KB instead of v1's 1.7 MB for the daily electricity datasets.
    return f"data.public.lu dataset {slug}", f"https://data.public.lu/api/2/datasets/{slug}/", None


UPSTREAMS: dict[str, tuple[str, str, dict[str, str] | None]] = {
    "catalog": ("data.public.lu API", f"{p.CATALOG}/site/", None),
    "downloads": ("data.public.lu downloads", "https://download.data.public.lu/", None),
    "geocoder": ("Geoportail geocoder", f"{p.GEOCODE}/search?{urlencode({'queryString': 'Luxembourg'})}", None),
    "geo_features": ("Geoportail OGC features", f"{p.FEATURES}/collections?f=json", None),
    "legilux": ("Legilux SPARQL", f"{p.LEGILUX}?{urlencode({'query': 'ASK { ?s ?p ?o }'})}",
                {"Accept": "application/sparql-results+json"}),
    "statec": ("STATEC LUSTAT", f"{p.STATEC}/dataflow/LU1/DF_D7100/latest",
               {"Accept": "application/vnd.sdmx.structure+xml;version=2.1"}),
    "vdl_parking": ("Ville de Luxembourg parking feed", p.VDL_PARKING, None),
    "vdl_maps": ("Ville de Luxembourg maps", "https://maps.vdl.lu/arcgis/rest/services/OPENDATA/GEOJSON/FeatureServer?f=json", None),
    "cfl_parking": ("CFL Park and Ride", f"{p.CFL_PARKING}/", None),
    "cita_traffic": ("CITA traffic sensors", f"{p.CITA_TRAFFIC}/trafficstatus_a6", {"Accept": "application/xml"}),
    "cita_events": ("CITA traffic events", p.CITA_EVENTS, {"Accept": "application/xml"}),
    "water_levels": ("Water levels export", p.WATER_LEVELS, {"Accept": "text/csv"}),
    "accessibility": ("Digital Accessibility Observatory", f"{p.ACCESSIBILITY}/key_figures", None),
    "meteolux_forecast": ("MeteoLux forecast API", f"{p.METEOLUX_FORECAST}?{urlencode({'lat': 49.6116, 'long': 6.1319, 'langcode': 'en'})}", None),
    "pharmacies": ("Pharmacie.lu on-duty feed", p.PHARMACY_ON_DUTY, {"Accept": "text/csv"}),
    "velok": ("Vël'OK stations", p.VELOK_STATIONS, {"Accept": "application/xml"}),
    "tenders": ("Public procurement portal", f"{p.PMP_TENDERS}?itemsPerPage=1&page=1", {"Accept": "application/ld+json"}),
    "lod": ("Lëtzebuerger Online Dictionnaire", f"{p.LOD_API}/en/word-of-the-day", None),
}
for _slug in (p.WEATHER_ALERTS_SLUG, p.AIR_DATASET, p.CHAMBER_BODIES_DATASET, p.GTFS_DATASET, p.WEATHER_OBS_SLUG,
              p.HOLIDAYS_SLUG, p.QUESTIONS_SLUG, p.HOUSING_SLUG, p.ELECTIONS_SLUG, p.CHARGY_SLUG, p.WASTE_SLUG,
              p.FUEL_PRICES_SLUG, p.ALERTS_SLUG, p.COLLEGE_SLUG, p.POPULATION_SLUG, p.ELECTRICITY_SLUG, p.FLEX_SLUG,
              p.VOTES_SLUG, p.DEPUTIES_SLUG, p.LOAD_SLUG, p.GENERATION_SLUG, p.FLOWS_SLUG, p.ADEM_SLUG):
    UPSTREAMS[f"dataset:{_slug}"] = _dataset(_slug)


def _via_catalog(*slugs: str) -> tuple[str, ...]:
    return ("catalog", "downloads", *(f"dataset:{slug}" for slug in slugs))


# Which upstreams each tool needs; a test keeps this in step with the registered tools.
TOOL_UPSTREAMS: dict[str, tuple[str, ...]] = {
    "search_datasets": ("catalog",),
    "get_dataset": ("catalog",),
    "geocode_address": ("geocoder",),
    "reverse_geocode": ("geocoder",),
    "list_geo_collections": ("geo_features",),
    "get_geo_features": ("geo_features",),
    "get_weather_alerts": _via_catalog(p.WEATHER_ALERTS_SLUG),
    "search_legislation": ("legilux",),
    "search_statistics": ("statec",),
    "get_statistics": ("statec",),
    "get_city_parking": ("vdl_parking",),
    "list_cfl_parking": ("cfl_parking",),
    "get_cfl_parking": ("cfl_parking",),
    "get_traffic": ("cita_traffic",),
    "get_water_levels": ("water_levels",),
    "get_air_quality": _via_catalog(p.AIR_DATASET),
    "search_chamber_bodies": _via_catalog(p.CHAMBER_BODIES_DATASET),
    "get_accessibility_figures": ("accessibility",),
    "get_accessibility_audits": ("accessibility",),
    "search_transit_stops": _via_catalog(p.GTFS_DATASET),
    "get_city_mobility": ("vdl_maps",),
    "get_weather_observations": _via_catalog(p.WEATHER_OBS_SLUG),
    "get_public_holidays": _via_catalog(p.HOLIDAYS_SLUG),
    "search_parliamentary_questions": _via_catalog(p.QUESTIONS_SLUG),
    "get_housing_prices": _via_catalog(p.HOUSING_SLUG),
    "get_election_results": _via_catalog(p.ELECTIONS_SLUG),
    "get_ev_charging": _via_catalog(p.CHARGY_SLUG),
    "get_waste_collections": _via_catalog(p.WASTE_SLUG),
    "get_weather_forecast": ("meteolux_forecast",),
    "get_fuel_prices": _via_catalog(p.FUEL_PRICES_SLUG),
    "get_public_alerts": _via_catalog(p.ALERTS_SLUG),
    "get_commune_leaders": _via_catalog(p.COLLEGE_SLUG),
    "get_commune_population": _via_catalog(p.POPULATION_SLUG),
    "get_pharmacies_on_duty": ("pharmacies",),
    "get_electricity_prices": _via_catalog(p.ELECTRICITY_SLUG),
    "get_carsharing": _via_catalog(p.FLEX_SLUG),
    "get_bike_sharing": ("velok",),
    "search_tenders": ("tenders",),
    "get_plenary_votes": _via_catalog(p.VOTES_SLUG),
    "get_deputies": _via_catalog(p.DEPUTIES_SLUG),
    "lookup_luxembourgish": ("lod",),
    "get_traffic_events": ("cita_events",),
    "get_electricity_grid": _via_catalog(p.LOAD_SLUG, p.GENERATION_SLUG, p.FLOWS_SLUG),
    "get_unemployment": _via_catalog(p.ADEM_SLUG),
}


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class StatusMonitor:
    """Probes every upstream at most once per REFRESH_SECONDS, however many people load the page.

    One failed probe only marks an upstream degraded; it takes two in a row to show it as down,
    so a single blip on a flaky government server does not paint the catalogue red.
    """

    def __init__(self, http: Any = None, *, refresh_seconds: int = REFRESH_SECONDS, clock: Any = time.time):
        self.http = http or HttpClient()
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._state: dict[str, dict[str, Any]] = {}
        self._checked_at: float | None = None
        self._lock = threading.Lock()
        self._refreshing = threading.Lock()
        self._first_round = threading.Event()

    def _probe(self, key: str) -> tuple[str, float, str | None]:
        _, url, headers = UPSTREAMS[key]
        started = time.monotonic()
        try:
            self.http.probe(url, headers, timeout=PROBE_TIMEOUT_SECONDS)
            return key, time.monotonic() - started, None
        except UpstreamError as exc:
            return key, time.monotonic() - started, str(exc)[:200]
        except Exception as exc:  # a probe must never take the status page down with it
            return key, time.monotonic() - started, f"probe failed: {type(exc).__name__}"

    def refresh(self) -> None:
        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="status-probe")
        futures = {pool.submit(self._probe, key): key for key in UPSTREAMS}
        done, _ = wait(futures, timeout=ROUND_DEADLINE_SECONDS)
        # Don't wait for stragglers: unstarted probes are cancelled and running ones finish on their own timeout.
        pool.shutdown(wait=False, cancel_futures=True)
        results = [future.result() if future in done else (key, ROUND_DEADLINE_SECONDS, "check timed out")
                   for future, key in futures.items()]
        now = self.clock()
        with self._lock:
            for key, elapsed, error in results:
                previous = self._state.get(key, {})
                failures = previous.get("consecutive_failures", 0) + 1 if error else 0
                if error:
                    status = "down" if failures >= 2 else "degraded"
                else:
                    status = "degraded" if elapsed > SLOW_SECONDS else "ok"
                self._state[key] = {
                    "status": status,
                    "latency_ms": round(elapsed * 1000),
                    "error": error,
                    "consecutive_failures": failures,
                    "checked_at": now,
                    "last_ok_at": previous.get("last_ok_at") if error else now,
                }
            self._checked_at = now
        self._first_round.set()

    def _refresh_in_background(self) -> None:
        if not self._refreshing.acquire(blocking=False):
            return  # another request is already refreshing

        def run() -> None:
            try:
                self.refresh()
            finally:
                self._refreshing.release()
                self._first_round.set()  # even a failed round must not leave first visitors waiting

        threading.Thread(target=run, name="status-refresh", daemon=True).start()

    def snapshot(self) -> dict:
        if self._checked_at is None:
            # First visit: start a round and give it a few seconds; past that, answer "unknown" now.
            self._refresh_in_background()
            self._first_round.wait(FIRST_WAIT_SECONDS)
        elif self.clock() - self._checked_at >= self.refresh_seconds:
            self._refresh_in_background()  # stale-while-revalidate: answer now with the last round
        with self._lock:
            upstreams = {
                key: {
                    "name": UPSTREAMS[key][0],
                    "url": UPSTREAMS[key][1],
                    "status": state["status"],
                    "latency_ms": state["latency_ms"],
                    "error": state["error"],
                    "checked_at": _iso(state["checked_at"]),
                    "last_ok_at": _iso(state["last_ok_at"]),
                }
                for key, state in self._state.items()
            }
            checked_at = self._checked_at
        tools = {}
        for name, keys in TOOL_UPSTREAMS.items():
            states = [upstreams.get(key, {}).get("status", "unknown") for key in keys]
            tools[name] = {"status": max(states, key=_RANK.__getitem__), "upstreams": list(keys)}
        summary = {state: 0 for state in _RANK}
        for tool in tools.values():
            summary[tool["status"]] += 1
        return {"checked_at": _iso(checked_at), "refresh_seconds": self.refresh_seconds,
                "summary": summary, "tools": tools, "upstreams": upstreams}
