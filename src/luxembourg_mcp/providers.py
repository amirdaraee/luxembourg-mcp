"""Adapters for official Luxembourg public-data services."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from xml.etree import ElementTree
from xml.parsers import expat
from typing import Any
from urllib.parse import quote, urlencode

from .http import HttpClient, UpstreamError

CATALOG = "https://data.public.lu/api/1"
# apiv3 302-redirects to apiv4; hardcoded fetches only follow same-host redirects.
GEOCODE = "https://apiv4.geoportail.lu/geocode"
FEATURES = "https://features.geoportail.lu"
WEATHER_ALERTS_SLUG = "meteolux-weather-warnings-for-the-grand-duchy-of-luxembourg-1"
LEGILUX = "https://data.legilux.public.lu/sparqlendpoint"
STATEC = "https://lustat.statec.lu/rest"
VDL_PARKING = "https://feed.vdl.lu/circulation/parking/feed.json"
CFL_PARKING = "https://pr-mobile-a.cfl.lu/OpenData/ParkAndRide"
CITA_TRAFFIC = "https://www.cita.lu/info_trafic/datex"
# inondations.lu now redirects to the portal homepage; the export moved to this file.
WATER_LEVELS = "https://inondations.public.lu/dam-assets/ctie/datas/Water-Levels-LocalTime.csv"
ACCESSIBILITY = "https://observatoire.accessibilite.public.lu/api/1"
AIR_DATASET = "air-quality-telemetric-network"
CHAMBER_BODIES_DATASET = "liste-organes-commissions-et-delegations"
GTFS_DATASET = "horaires-et-arrets-des-transport-publics-gtfs"
VDL_MOBILITY_LAYERS = {
    "park_and_bike": 3,
    "park_and_ride": 4,
    "covered_parking": 5,
    "surface_parking": 6,
    "accessible_parking": 7,
    "bike_rentals": 8,
}
CITA_ROADS = {"a3", "a4", "a6", "a7", "a13", "b40"}
DATA_PUBLIC_RESOURCE_HOSTS = frozenset({"download.data.public.lu"})
WEATHER_OBS_SLUG = "hvd-annex-3-meteorological-live-weather-observations-at-luxembourg-airport-ellx"
HOLIDAYS_SLUG = "jours-feries-legaux-au-luxembourg"
QUESTIONS_SLUG = "liste-des-questions-parlementaires"
HOUSING_SLUG = "prix-annonces-des-logements-par-commune"
ELECTIONS_SLUG = "elections-legislatives-2023-donnees-officieuses"
CHARGY_SLUG = "bornes-de-chargement-publiques-pour-voitures-electriques"
WASTE_SLUG = "waste-municipal-waste-collection-calendars-dechets-calendriers-municipaux-de-collecte-des-dechets"
FUEL_PRICES_SLUG = "comparaison-des-prix-de-carburants-par-motorisation"
ALERTS_SLUG = "alertes-du-systeme-lu-alert"
COLLEGE_SLUG = "college-des-bourgmestre-et-echevins-depuis-les-elections-communales-2023"
POPULATION_SLUG = "registre-national-des-personnes-physiques-rnpp-population-par-commune-population-per-municipality"
ELECTRICITY_SLUG = "electricity-in-luxembourg-day-ahead-prices"
FLEX_SLUG = "flex-carsharing-by-cfl-1"
# MeteoLux publishes the forecast only on its app API (CC0, documented); /hvd carries observations.
METEOLUX_FORECAST = "https://metapi.ana.lu/api/v1/metapp/weather"
PHARMACY_ON_DUTY = "https://pharmacie.lu/feed-garde-csv"
VELOK_STATIONS = "https://www.velok.lu/api-proxy.php"
PMP_TENDERS = "https://pmp.b2g.etat.lu/api/v2/consultations"
# The Chargy dataset's resource URL lives on my.chargy.lu and carries a key that
# Chargy itself publishes openly in the national catalog, so no user key is needed.
CHARGY_RESOURCE_HOSTS = DATA_PUBLIC_RESOURCE_HOSTS | {"my.chargy.lu"}
METEOLUX_SENSOR_LABELS = {
    "st": "air temperature (°C)",
    "std": "dew point temperature (°C)",
    "stw": "wet-bulb temperature (°C)",
    "stags": "grass temperature (°C)",
    "su": "relative humidity (%)",
    "svp": "vapour pressure (hPa)",
    "spsl": "sea-level pressure (hPa)",
    "sqnh": "QNH pressure (hPa)",
    "sqfe": "QFE pressure (hPa)",
    "srr": "precipitation rate",
    "svv": "visibility",
}
METEOLUX_SENSOR_PREFIXES = {
    "s2ffgust": "wind gust speed",
    "s2ddgust": "wind gust direction (degrees)",
    "s2ff": "wind speed",
    "s2dd": "wind direction (degrees)",
    "svv": "visibility",
    "sh": "cloud base height",
}
METEOLUX_RUNWAYS = {"rwy060": "runway 06", "rwy240": "runway 24", "rwymd0": "mid-runway"}
MAX_GTFS_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100


def _meteolux_label(sensor_id: str) -> str | None:
    if sensor_id in METEOLUX_SENSOR_LABELS:
        return METEOLUX_SENSOR_LABELS[sensor_id]
    base, _, suffix = sensor_id.partition("_")
    label = METEOLUX_SENSOR_PREFIXES.get(base)
    if label is None:
        return None
    runway = METEOLUX_RUNWAYS.get(suffix)
    return f"{label}, {runway}" if runway else label


class _PrologDone(Exception):
    pass


def _parse_xml(payload: bytes) -> ElementTree.Element:
    """ElementTree.fromstring that refuses DTD entity declarations (entity-expansion bombs)."""
    probe = expat.ParserCreate()

    def reject_entity(*_: Any) -> None:
        raise ElementTree.ParseError("XML entity declarations are not allowed")

    def stop_at_root(*_: Any) -> None:
        raise _PrologDone

    # Entities can only be declared before the root element, so the probe stops there.
    probe.EntityDeclHandler = reject_entity
    probe.StartElementHandler = stop_at_root
    try:
        probe.Parse(payload, True)
    except (_PrologDone, expat.ExpatError):
        pass  # syntax errors are reported by the real parse below
    return ElementTree.fromstring(payload)


def _require_path_segments(value: str, name: str) -> None:
    """Reject values that would become '.' or '..' URL path segments once interpolated."""
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"{name} must not contain '.' or '..' path segments")


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _xlsx_sheet_rows(payload: bytes, sheet_name: str | None) -> tuple[str, list[str], list[dict[str, str]]]:
    """Minimal stdlib XLSX reader: returns (chosen sheet, all sheet names, rows as {column: value})."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            workbook = _parse_xml(_read_bounded_zip_member(archive, "xl/workbook.xml"))
            rels = {
                rel.get("Id"): rel.get("Target")
                for rel in _parse_xml(_read_bounded_zip_member(archive, "xl/_rels/workbook.xml.rels"))
            }
            sheets = {s.get("name"): rels.get(s.get(_XLSX_RID)) for s in workbook.iter(f"{_XLSX_NS}sheet")}
            names = [name for name in sheets if name]
            chosen = sheet_name if sheet_name is not None else max(names)
            if chosen not in sheets or not sheets[chosen]:
                raise ValueError(f"sheet must be one of: {', '.join(names)}")
            target = sheets[chosen]
            path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = _parse_xml(_read_bounded_zip_member(archive, "xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")) for si in strings]
            sheet = _parse_xml(_read_bounded_zip_member(archive, path))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise UpstreamError("Upstream returned an invalid XLSX workbook") from exc
    rows = []
    for row in sheet.iter(f"{_XLSX_NS}row"):
        cells: dict[str, str] = {}
        for cell in row.iter(f"{_XLSX_NS}c"):
            raw = cell.findtext(f"{_XLSX_NS}v")
            if raw is None:
                continue
            if cell.get("t") == "s" and raw.isdigit() and int(raw) < len(shared):
                value = shared[int(raw)]
            else:
                value = raw
            column = re.match(r"[A-Z]+", cell.get("r") or "")
            if column:
                cells[column.group()] = value
        if cells:
            rows.append(cells)
    return chosen, names, rows


def _price_number(value: str | None) -> int | float | None:
    if value is None or value.strip() in ("", "*"):
        return None
    return _number(value)


def _fold(text: str) -> str:
    """Casefold and strip accents so 'Ettelbruck' matches 'Ettelbrück'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _organization(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def _dataset_summary(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "slug": item.get("slug"),
        "title": item.get("title"),
        "description": item.get("description"),
        "organization": _organization(item.get("organization")),
        "last_modified": item.get("last_modified"),
        "page": item.get("page") or (f"https://data.public.lu/en/datasets/{item.get('slug')}/" if item.get("slug") else None),
    }


class LuxembourgData:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()
        self._cache: dict[str, tuple[float, int, Any]] = {}
        self._cache_lock = threading.Lock()
        self._cache_key_locks: dict[str, threading.Lock] = {}

    def _fresh(self, key: str) -> tuple[bool, Any]:
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < cached[1]:
            return True, cached[2]
        return False, None

    def _cached(self, key: str, ttl: int, loader: Any) -> Any:
        with self._cache_lock:
            hit, value = self._fresh(key)
            if hit:
                return value
            key_lock = self._cache_key_locks.setdefault(key, threading.Lock())
        # Single-flight per key: concurrent callers on a cold cache wait for one
        # upstream download instead of each fetching (and parsing) it again.
        with key_lock:
            try:
                with self._cache_lock:
                    hit, value = self._fresh(key)
                if hit:
                    return value
                value = loader()
                now = time.monotonic()
                with self._cache_lock:
                    for stale_key in [k for k, entry in self._cache.items() if now - entry[0] >= entry[1]]:
                        self._cache.pop(stale_key, None)
                    if key not in self._cache and len(self._cache) >= 32:
                        oldest = min(self._cache, key=lambda item: self._cache[item][0])
                        self._cache.pop(oldest, None)
                    self._cache[key] = (now, ttl, value)
                return value
            finally:
                with self._cache_lock:
                    if self._cache_key_locks.get(key) is key_lock:
                        del self._cache_key_locks[key]

    @staticmethod
    def _decode_csv(payload: bytes, delimiter: str | None = None) -> list[dict[str, str]]:
        text = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if text is None:
            text = payload.decode("utf-8", errors="replace")
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;").delimiter
            except csv.Error:
                delimiter = ","
        return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))

    def search_datasets(self, query: str, page: int = 1, page_size: int = 10) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        page_size = min(max(page_size, 1), 50)
        url = f"{CATALOG}/datasets/?{urlencode({'q': query, 'page': page, 'page_size': page_size})}"
        data = self.http.get_json(url)
        return {
            "total": data.get("total", 0),
            "page": data.get("page", page),
            "page_size": data.get("page_size", page_size),
            "datasets": [_dataset_summary(item) for item in data.get("data", [])],
            "source": url,
        }

    def get_dataset(self, dataset_id_or_slug: str) -> dict:
        if not dataset_id_or_slug.strip():
            raise ValueError("dataset_id_or_slug must not be empty")
        _require_path_segments(dataset_id_or_slug, "dataset_id_or_slug")
        url = f"{CATALOG}/datasets/{quote(dataset_id_or_slug, safe='')}/"
        item = self.http.get_json(url)
        result = _dataset_summary(item)
        result.update({
            "license": item.get("license"),
            "tags": item.get("tags", []),
            "resources": [
                {
                    "id": resource.get("id"),
                    "title": resource.get("title"),
                    "description": resource.get("description"),
                    "format": resource.get("format"),
                    "mime": resource.get("mime"),
                    "url": resource.get("url"),
                    "latest": resource.get("latest"),
                    "last_modified": resource.get("last_modified"),
                }
                for resource in item.get("resources", [])
            ],
            "source": url,
        })
        return result

    def geocode_address(self, query: str) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        url = f"{GEOCODE}/search?{urlencode({'queryString': query})}"
        result = self.http.get_json(url)
        result["source"] = url
        return result

    def reverse_geocode(self, latitude: float, longitude: float) -> dict:
        if not 49.0 <= latitude <= 50.5 or not 5.0 <= longitude <= 7.5:
            raise ValueError("coordinates must be in or near Luxembourg")
        url = f"{GEOCODE}/reverse?{urlencode({'lat': latitude, 'lon': longitude})}"
        result = self.http.get_json(url)
        result["source"] = url
        return result

    def list_geo_collections(self, query: str | None = None, limit: int = 20) -> dict:
        url = f"{FEATURES}/collections?f=json"
        collections = self.http.get_json(url).get("collections", [])
        if query:
            needle = _fold(query)
            collections = [
                item for item in collections
                if needle in _fold(" ".join(
                    str(value) for value in (
                        item.get("title", ""),
                        item.get("description", ""),
                        *(item.get("keywords") or []),
                    )
                ))
            ]
        total = len(collections)
        return {
            "total": total,
            "collections": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "keywords": item.get("keywords", []),
                    "extent": item.get("extent"),
                }
                for item in collections[: min(max(limit, 1), 100)]
            ],
            "source": url,
        }

    def get_geo_features(
        self,
        collection_id: str,
        limit: int = 10,
        bbox: list[float] | None = None,
    ) -> dict:
        if not collection_id or ".." in collection_id:
            raise ValueError("invalid collection_id")
        _require_path_segments(collection_id, "collection_id")
        params: dict[str, Any] = {"f": "json", "limit": min(max(limit, 1), 100)}
        if bbox is not None:
            if len(bbox) != 4 or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                raise ValueError("bbox must be [west, south, east, north]")
            params["bbox"] = ",".join(str(value) for value in bbox)
        encoded_id = "/".join(quote(part, safe="") for part in collection_id.split("/"))
        url = f"{FEATURES}/collections/{encoded_id}/items?{urlencode(params)}"
        result = self.http.get_json(url)
        result["source"] = url
        return result

    def get_weather_alerts(self, language: str = "en") -> dict:
        language = language.lower()
        if language not in {"en", "fr", "de", "lu"}:
            raise ValueError("language must be one of en, fr, de, lu")
        dataset = self.get_dataset(WEATHER_ALERTS_SLUG)
        preferred = f"{language}-data-alerts.csv" if language != "lu" else "data-alerts.csv"
        resources = dataset.get("resources", [])
        resource = next((r for r in resources if (r.get("title") or "").lower() == preferred), None)
        if resource is None and language == "en":
            resource = next((r for r in resources if "en-data-alerts" in (r.get("url") or "").lower()), None)
        if resource is None:
            raise UpstreamError(f"MeteoLux has no {language} alert resource at present")
        resource_url = resource.get("url")
        if not resource_url:
            raise UpstreamError("MeteoLux alert resource has no download URL")
        raw, charset = self.http.get_bytes(resource_url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
        text = raw.decode(charset, errors="replace")
        # MeteoLux prefixes the CSV with an Excel "sep=;" hint and a "created;<timestamp>"
        # metadata line before the real header row.
        lines = text.splitlines()
        created = None
        while lines and (lines[0].lower().startswith("sep=") or lines[0].lower().startswith("created")):
            if lines[0].lower().startswith("created") and ";" in lines[0]:
                created = lines[0].split(";", 1)[1].strip()
            lines.pop(0)
        text = "\n".join(lines)
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        alerts = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        return {
            "language": language,
            "created": created,
            "count": len(alerts),
            "alerts": alerts,
            "source": resource_url,
            "dataset": dataset.get("page"),
        }

    def search_legislation(self, query: str, limit: int = 10) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        limit = min(max(limit, 1), 50)
        jolux = "http://data.legilux.public.lu/resource/ontology/jolux#"
        literal = json.dumps(query.strip(), ensure_ascii=False)
        sparql = f"""SELECT DISTINCT ?work ?title ?date WHERE {{
          ?work <{jolux}isRealizedBy> ?expression .
          ?expression <{jolux}title> ?title .
          OPTIONAL {{ ?work <{jolux}publicationDate> ?date }}
          FILTER(CONTAINS(LCASE(STR(?title)), LCASE({literal})))
        }} ORDER BY DESC(?date) LIMIT {limit}"""
        url = f"{LEGILUX}?{urlencode({'query': sparql})}"
        payload, charset = self.http.get_bytes(url, {"Accept": "application/sparql-results+json"})
        try:
            data = json.loads(payload.decode(charset))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("Legilux returned invalid SPARQL JSON") from exc
        results = []
        for binding in data.get("results", {}).get("bindings", []):
            results.append({name: value.get("value") for name, value in binding.items()})
        return {"count": len(results), "results": results, "source": url}

    def _statec_dataflows(self) -> list[dict]:
        def load() -> list[dict]:
            url = f"{STATEC}/dataflow/LU1/all/latest"
            payload, _ = self.http.get_bytes(url, {"Accept": "application/vnd.sdmx.structure+xml;version=2.1"})
            try:
                root = _parse_xml(payload)
            except ElementTree.ParseError as exc:
                raise UpstreamError("STATEC returned invalid SDMX XML") from exc
            common = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common}"
            structure = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure}"
            xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
            flows = []
            for node in root.findall(f".//{structure}Dataflow"):
                names = {child.get(xml_lang, ""): child.text for child in node.findall(f"{common}Name")}
                descriptions = {child.get(xml_lang, ""): child.text for child in node.findall(f"{common}Description")}
                flows.append({
                    "id": node.get("id"),
                    "version": node.get("version"),
                    "name": names.get("en") or names.get("fr"),
                    "name_fr": names.get("fr"),
                    "description": descriptions.get("en") or descriptions.get("fr"),
                })
            return flows
        return self._cached("statec-dataflows", 3600, load)

    def search_statistics(self, query: str, limit: int = 10) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        needle = _fold(query)
        matches = [
            item for item in self._statec_dataflows()
            if needle in _fold(" ".join(str(value or "") for value in item.values()))
        ][: min(max(limit, 1), 50)]
        return {
            "count": len(matches),
            "dataflows": matches,
            "source": f"{STATEC}/dataflow/LU1/all/latest",
        }

    def get_statistics(
        self,
        dataflow_id: str,
        key: str = "all",
        last_n_observations: int = 5,
        max_rows: int = 500,
    ) -> dict:
        if not re.fullmatch(r"DF_[A-Za-z0-9_]+", dataflow_id):
            raise ValueError("dataflow_id must look like DF_D7100")
        if not re.fullmatch(r"[A-Za-z0-9+._-]+", key):
            raise ValueError("key contains unsupported characters")
        _require_path_segments(key, "key")
        last_n_observations = min(max(last_n_observations, 1), 100)
        max_rows = min(max(max_rows, 1), 2000)
        params = {"lastNObservations": last_n_observations, "dimensionAtObservation": "AllDimensions"}
        url = f"{STATEC}/data/LU1,{quote(dataflow_id)}/{quote(key)}?{urlencode(params)}"
        accept = "application/vnd.sdmx.data+csv;version=2.0;labels=both"
        payload, _ = self.http.get_bytes(url, {"Accept": accept})
        rows = self._decode_csv(payload)
        return {"count": min(len(rows), max_rows), "total_rows": len(rows), "truncated": len(rows) > max_rows, "rows": rows[:max_rows], "source": url}

    def get_city_parking(self, query: str | None = None, available_only: bool = False) -> dict:
        data = self.http.get_json(VDL_PARKING)
        parking = list((data.get("parking") or {}).values())
        if query:
            needle = _fold(query)
            parking = [item for item in parking if needle in _fold(json.dumps(item, ensure_ascii=False))]
        if available_only:
            parking = [item for item in parking if item.get("ouvert") and (item.get("actuel") or 0) > 0]
        return {"updated": data.get("last_build_date"), "count": len(parking), "parking": parking, "source": VDL_PARKING}

    def list_cfl_parking(self) -> dict:
        url = f"{CFL_PARKING}/"
        data = self.http.get_json_value(url)
        if not isinstance(data, list):
            raise UpstreamError("CFL returned an unexpected parking list")
        return {"count": len(data), "parking": data, "source": url}

    def get_cfl_parking(self, parking_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parking_id):
            raise ValueError("invalid parking_id")
        url = f"{CFL_PARKING}/{quote(parking_id)}"
        return {"parking": self.http.get_json(url), "source": url}

    def get_traffic(self, road: str = "a6") -> dict:
        road = road.lower()
        if road not in CITA_ROADS:
            raise ValueError(f"road must be one of {', '.join(sorted(CITA_ROADS))}")
        url = f"{CITA_TRAFFIC}/trafficstatus_{road}"
        payload, _ = self.http.get_bytes(url, {"Accept": "application/xml"})
        try:
            root = _parse_xml(payload)
        except ElementTree.ParseError as exc:
            raise UpstreamError("CITA returned invalid DATEX II XML") from exc
        stations = []
        for site in root.findall(".//{*}siteMeasurements"):
            ref = site.find("{*}measurementSiteReference")
            stations.append({
                "id": ref.get("id") if ref is not None else None,
                "time": site.findtext("{*}measurementTimeDefault"),
                "road": site.findtext(".//{*}roadNumber"),
                "direction": site.findtext(".//{*}directionBoundOnLinearSection"),
                "latitude": _number(site.findtext(".//{*}latitude")),
                "longitude": _number(site.findtext(".//{*}longitude")),
                "average_speed_kmh": _number(site.findtext(".//{*}speed")),
                "occupancy_percent": _number(site.findtext(".//{*}percentage")),
                "vehicle_flow_per_hour": _number(site.findtext(".//{*}vehicleFlowRate")),
            })
        return {"road": road.upper(), "count": len(stations), "stations": stations, "source": url}

    def get_water_levels(self, station: str | None = None) -> dict:
        payload, _ = self.http.get_bytes(WATER_LEVELS, {"Accept": "text/csv"})
        rows = [
            row for row in csv.reader(io.StringIO(payload.decode("utf-8-sig", errors="replace")))
            if any(cell.strip() for cell in row)
        ]
        # One row per station; the header carries a column per quarter-hourly timestamp.
        header = next((row for row in rows if row and row[0].strip().casefold() == "name"), None)
        timestamps = header[3:] if header else []
        results = []
        for row in rows[1:] if header else []:
            readings = [(timestamps[index], value) for index, value in enumerate(row[3:])
                        if index < len(timestamps) and value.strip() and _water_timestamp(timestamps[index]) is not None]
            if not readings:
                continue
            measured_at, value = max(readings, key=lambda item: _water_timestamp(item[0]))
            name = row[0]
            if station and _fold(station) not in _fold(name):
                continue
            results.append({
                "name": name,
                "station_number": row[1] or None,
                "unit": row[2] or None,
                "value": _number(value),
                "measured_at": measured_at,
            })
        if not results:
            if station:
                raise ValueError(f"no water-level station matched: {station}")
            raise UpstreamError("Water-level export contained no measurements")
        latest = max(results, key=lambda item: _water_timestamp(item["measured_at"]))
        return {"measured_at": latest["measured_at"], "count": len(results), "stations": results, "source": WATER_LEVELS}

    def get_air_quality(self, city: str | None = None) -> dict:
        dataset = self.get_dataset(AIR_DATASET)
        resource = next((item for item in dataset.get("resources", []) if item.get("format") == "json" and "1hour" in (item.get("title") or "")), None)
        if resource is None or not resource.get("url"):
            raise UpstreamError("No current national air-quality resource was found")
        resource_url = resource["url"]
        def load() -> dict:
            data = self.http.get_json_value(resource_url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            if not isinstance(data, list) or not data:
                raise UpstreamError("Air-quality resource contained no observations")
            return data[-1]
        latest = self._cached(f"air:{resource_url}", 600, load)
        stations = latest.get("station", [])
        if city:
            needle = _fold(city)
            stations = [item for item in stations if needle in _fold(item.get("adr_city") or "")]
        return {"generated": latest.get("generated"), "count": len(stations), "stations": stations, "source": resource_url, "dataset": dataset.get("page")}

    def search_chamber_bodies(self, query: str, limit: int = 20) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        dataset = self.get_dataset(CHAMBER_BODIES_DATASET)
        resource = next((item for item in dataset.get("resources", []) if item.get("format") == "csv"), None)
        if resource is None or not resource.get("url"):
            raise UpstreamError("No Chamber bodies CSV resource was found")
        url = resource["url"]
        payload, _ = self.http.get_bytes(url, {"Accept": "text/csv"}, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
        rows = self._decode_csv(payload)
        needle = _fold(query)
        results = [row for row in rows if needle in _fold(" ".join(row.values()))][: min(max(limit, 1), 100)]
        return {"count": len(results), "results": results, "source": url, "dataset": dataset.get("page")}

    def get_accessibility_figures(self) -> dict:
        url = f"{ACCESSIBILITY}/key_figures"
        data = self.http.get_json_value(url)
        return {"figures": data[0] if isinstance(data, list) and data else data, "source": url}

    def get_accessibility_audits(self, limit: int = 10) -> dict:
        limit = min(max(limit, 1), 100)
        url = f"{ACCESSIBILITY}/last_audits?{urlencode({'limit': limit})}"
        data = self.http.get_json_value(url)
        if not isinstance(data, list):
            raise UpstreamError("Accessibility Observatory returned an unexpected audit list")
        return {"count": len(data), "audits": data, "source": url}

    def search_transit_stops(self, query: str, limit: int = 20) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        dataset = self.get_dataset(GTFS_DATASET)
        resource = next((item for item in dataset.get("resources", []) if item.get("format") == "zip" and "gtfs" in (item.get("title") or "").lower()), None)
        if resource is None or not resource.get("url"):
            raise UpstreamError("No current official GTFS resource was found")
        url = resource["url"]
        def load() -> list[dict[str, str]]:
            payload, _ = self.http.get_bytes(url, {"Accept": "application/zip"}, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    return self._decode_csv(_read_bounded_zip_member(archive, "stops.txt"))
            except (KeyError, zipfile.BadZipFile) as exc:
                raise UpstreamError("Official GTFS archive has no valid stops.txt") from exc
        stops = self._cached(f"gtfs:{url}", 3600, load)
        needle = _fold(query)
        matches = [stop for stop in stops if needle in _fold(" ".join(stop.values()))][: min(max(limit, 1), 100)]
        return {"count": len(matches), "stops": matches, "source": url, "dataset": dataset.get("page")}

    def _dataset_resource(self, slug: str, *, format: str, title_keyword: str | None = None) -> tuple[dict, dict]:
        dataset = self.get_dataset(slug)
        resource = next(
            (
                item for item in dataset.get("resources", [])
                if item.get("format") == format
                and (title_keyword is None or title_keyword in _fold(item.get("title") or ""))
                and item.get("url")
            ),
            None,
        )
        if resource is None:
            raise UpstreamError(f"No current {format} resource was found in dataset {slug}")
        return dataset, resource

    def get_weather_observations(self) -> dict:
        dataset, resource = self._dataset_resource(WEATHER_OBS_SLUG, format="json")
        data = self.http.get_json_value(resource["url"], allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise UpstreamError("MeteoLux observations had an unexpected shape")
        observations = [
            {"id": item.get("id"), "label": _meteolux_label(str(item.get("id") or "")), "value": item.get("value")}
            for item in data["data"]
        ]
        return {
            "station": "Luxembourg-Airport (ELLX)",
            "measured_at": data.get("timestamp"),
            "count": len(observations),
            "observations": observations,
            "source": resource["url"],
            "dataset": dataset.get("page"),
        }

    def get_public_holidays(self, year: int | None = None) -> dict:
        dataset, resource = self._dataset_resource(HOLIDAYS_SLUG, format="json")
        data = self.http.get_json_value(resource["url"], allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
        if not isinstance(data, list):
            raise UpstreamError("Holiday list had an unexpected shape")
        holidays = [item for item in data if year is None or item.get("year") == year]
        return {"year": year, "count": len(holidays), "holidays": holidays,
                "source": resource["url"], "dataset": dataset.get("page")}

    def search_parliamentary_questions(self, query: str, limit: int = 10) -> dict:
        if not query.strip():
            raise ValueError("query must not be empty")
        limit = min(max(limit, 1), 50)
        dataset, resource = self._dataset_resource(QUESTIONS_SLUG, format="csv")
        url = resource["url"]

        def load() -> list[dict[str, str]]:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            return self._decode_csv(payload)

        rows = self._cached(f"chd-questions:{url}", 3600, load)
        needle = _fold(query)
        matches = [row for row in rows if needle in _fold(" ".join(str(value) for value in row.values() if value))]
        matches.sort(key=lambda row: row.get("question_chd_entry_date") or "", reverse=True)
        return {"count": len(matches[:limit]), "total_matches": len(matches),
                "questions": matches[:limit], "source": url, "dataset": dataset.get("page")}

    def get_housing_prices(self, property_type: str = "apartment", commune: str | None = None, year: str | None = None) -> dict:
        if property_type not in {"apartment", "house"}:
            raise ValueError("property_type must be apartment or house")
        if year is not None and not re.fullmatch(r"20\d\d", year):
            raise ValueError("year must look like 2025")
        keyword = "appartements" if property_type == "apartment" else "maisons"
        dataset, resource = self._dataset_resource(HOUSING_SLUG, format="xlsx", title_keyword=keyword)
        url = resource["url"]

        def load() -> bytes:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            return payload

        payload = self._cached(f"housing:{url}", 3600, load)
        chosen, years, raw_rows = _xlsx_sheet_rows(payload, year)
        header_index = next(
            (index for index, row in enumerate(raw_rows) if _fold(row.get("C", "")) == "commune"), None)
        if header_index is None:
            raise UpstreamError("Housing workbook had an unexpected layout")
        rows = []
        for row in raw_rows[header_index + 1:]:
            name = row.get("C")
            if not name:
                continue
            rows.append({
                "commune": name,
                "offers": _price_number(row.get("D")),
                "average_price_eur": _price_number(row.get("E")),
                "average_price_per_m2_eur": _price_number(row.get("F")),
            })
        if commune:
            needle = _fold(commune)
            rows = [row for row in rows if needle in _fold(row["commune"])]
        return {"property_type": property_type, "year": chosen, "available_years": sorted(years),
                "count": len(rows), "rows": rows,
                "note": "Prices are masked (null) by the source for communes with under 30 listings.",
                "source": url, "dataset": dataset.get("page")}

    def get_election_results(self) -> dict:
        dataset, resource = self._dataset_resource(ELECTIONS_SLUG, format="xml")
        url = resource["url"]

        def load() -> dict:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            try:
                root = _parse_xml(payload)
            except ElementTree.ParseError as exc:
                raise UpstreamError("Election results were not valid XML") from exc

            def lists_of(entity):
                results = entity.find("{*}resultats")
                if results is None:
                    return []
                out = []
                for liste in results.findall(".//{*}liste"):
                    out.append({
                        "number": _number(liste.get("numero")),
                        "party": liste.findtext("{*}noms/{*}nom"),
                        "abbreviation": liste.findtext("{*}sigles/{*}sigle"),
                        "votes": _number(liste.get("suffragesTotal")),
                        "percentage": _number(liste.get("pourcentage")),
                        "seats": _number(liste.get("mandatsAttribues")),
                    })
                return out

            country = root.find("{*}entite[@type='PAYS']")
            if country is None:
                raise UpstreamError("Election results had an unexpected shape")
            stats = country.find("{*}statistiques")
            voters = stats.find("{*}electeurs") if stats is not None else None
            ballots = stats.find("{*}bulletins") if stats is not None else None
            circonscriptions = []
            for entity in country.findall(".//{*}entite"):
                if entity.get("circonscriptionElectorale") == "true":
                    circonscriptions.append({"name": entity.findtext("{*}nom"), "lists": lists_of(entity)})
            return {
                "election": root.findtext("{*}nom"),
                "date": root.findtext("{*}dateElection"),
                "status": "données officieuses (unofficial machine-readable results)",
                "national": {
                    "registered_voters": _number(voters.get("inscrits")) if voters is not None else None,
                    "blank_ballots": _number(ballots.get("blancs")) if ballots is not None else None,
                    "lists": lists_of(country),
                },
                "circonscriptions": circonscriptions,
                "source": url,
                "dataset": dataset.get("page"),
            }

        return self._cached(f"elections:{url}", 3600, load)

    def get_ev_charging(self, query: str | None = None, available_only: bool = False) -> dict:
        dataset, resource = self._dataset_resource(CHARGY_SLUG, format="kml")
        url = resource["url"]

        def load() -> list[dict]:
            # Chargy's endpoint content-negotiates strictly and rejects our default
            # JSON-preferring Accept header with HTTP 406.
            accept = {"Accept": "application/vnd.google-earth.kml+xml, application/xml;q=0.9, */*;q=0.5"}
            payload, _ = self.http.get_bytes(url, accept, allowed_hosts=CHARGY_RESOURCE_HOSTS)
            try:
                root = _parse_xml(payload)
            except ElementTree.ParseError as exc:
                raise UpstreamError("Chargy returned invalid KML") from exc
            stations = []
            for placemark in root.findall(".//{*}Placemark"):
                description = placemark.findtext("{*}description") or ""
                available_match = re.search(r"(\d+)</b>\s*available", description)
                occupied_match = re.search(r"(\d+)</b>\s*occupied", description)
                points = placemark.findtext(".//{*}Data[@name='CPnum']/{*}value")
                coordinates = (placemark.findtext(".//{*}coordinates") or "").strip().split(",")
                stations.append({
                    "name": placemark.findtext("{*}name"),
                    "address": placemark.findtext("{*}address"),
                    "available": (placemark.findtext("{*}styleUrl") or "") == "#AVAILABLE",
                    "charging_points": _number(points),
                    "available_connectors": int(available_match.group(1)) if available_match else None,
                    "occupied_connectors": int(occupied_match.group(1)) if occupied_match else None,
                    "longitude": _number(coordinates[0]) if len(coordinates) >= 2 else None,
                    "latitude": _number(coordinates[1]) if len(coordinates) >= 2 else None,
                })
            return stations

        stations = self._cached(f"chargy:{url}", 300, load)
        if query:
            needle = _fold(query)
            stations = [s for s in stations if needle in _fold(f"{s.get('name') or ''} {s.get('address') or ''}")]
        if available_only:
            stations = [s for s in stations if s.get("available")]
        return {"count": len(stations), "stations": stations, "source": url, "dataset": dataset.get("page")}

    def get_waste_collections(self, commune: str, street: str | None = None,
                              waste_type: str | None = None, limit: int = 20) -> dict:
        if not commune.strip():
            raise ValueError("commune must not be empty")
        limit = min(max(limit, 1), 100)
        dataset, resource = self._dataset_resource(WASTE_SLUG, format="csv")
        url = resource["url"]

        def load() -> list[dict[str, str]]:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            rows = self._decode_csv(payload, delimiter=";")
            if not any(row.get("Commune") for row in rows):
                raise UpstreamError("Waste calendar CSV had an unexpected layout")
            return rows

        rows = self._cached(f"waste:{url}", 3600, load)
        communes = sorted({row["Commune"] for row in rows if row.get("Commune")})
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

    def get_city_mobility(self, category: str) -> dict:
        layer = VDL_MOBILITY_LAYERS.get(category)
        if layer is None:
            raise ValueError(f"category must be one of {', '.join(VDL_MOBILITY_LAYERS)}")
        params = {"where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson"}
        url = f"https://maps.vdl.lu/arcgis/rest/services/OPENDATA/GEOJSON/FeatureServer/{layer}/query?{urlencode(params)}"
        data = self.http.get_json(url)
        return {"category": category, "count": len(data.get("features", [])), "features": data.get("features", []), "source": url}

    def _latest_resource(self, slug: str, fmt: str, ttl: int = 600) -> tuple[dict, dict]:
        """Newest resource of a dataset that publishes one file per day/quarter (cached: the metadata is large)."""
        dataset = self._cached(f"dataset:{slug}", ttl, lambda: self.get_dataset(slug))
        resources = [item for item in dataset.get("resources", []) if item.get("format") == fmt and item.get("url")]
        if not resources:
            raise UpstreamError(f"No current {fmt} resource was found in dataset {slug}")
        return dataset, max(resources, key=lambda item: item.get("last_modified") or "")

    def get_weather_forecast(self, latitude: float = 49.6116, longitude: float = 6.1319, language: str = "en") -> dict:
        if not 49.0 <= latitude <= 50.5 or not 5.0 <= longitude <= 7.5:
            raise ValueError("coordinates must be in or near Luxembourg")
        language = language.lower()
        if language not in {"en", "fr", "de", "lb"}:
            raise ValueError("language must be one of en, fr, de, lb")
        url = f"{METEOLUX_FORECAST}?{urlencode({'lat': latitude, 'long': longitude, 'langcode': language})}"
        data = self.http.get_json(url)
        forecast = data.get("forecast") or {}

        def entry(item: dict) -> dict:
            wind = item.get("wind") or {}
            shaped = {
                "time": item.get("date"),
                "conditions": (item.get("icon") or {}).get("name"),
                "wind_direction": wind.get("direction") or None,
                "wind_speed_kmh": wind.get("speed"),
                "wind_gusts_kmh": wind.get("gusts"),
                "rain_mm": item.get("rain"),
                "snow_cm": item.get("snow"),
            }
            for key, source in (("temperature_c", "temperature"), ("temperature_min_c", "temperatureMin"), ("temperature_max_c", "temperatureMax")):
                value = (item.get(source) or {}).get("temperature")
                if value is not None:
                    shaped[key] = value
            for key in ("sunshine", "uvIndex"):
                if item.get(key) is not None:
                    shaped["sunshine_hours" if key == "sunshine" else "uv_index"] = item[key]
            return shaped

        ephemeris = data.get("ephemeris") or {}
        return {
            "place": (data.get("city") or {}).get("name"),
            "canton": (data.get("city") or {}).get("canton"),
            "current": entry(forecast.get("current") or {}),
            "hourly": [entry(item) for item in (forecast.get("hourly") or [])],
            "daily": [entry(item) for item in (forecast.get("daily") or [])],
            "sunrise": ephemeris.get("sunrise"),
            "sunset": ephemeris.get("sunset"),
            "uv_index": ephemeris.get("uvIndex"),
            "active_warnings": len(data.get("vigilances") or []),
            "source": url,
        }

    def get_fuel_prices(self, months: int = 6) -> dict:
        months = min(max(months, 1), 24)
        dataset, resource = self._dataset_resource(FUEL_PRICES_SLUG, format="csv")
        url = resource["url"]
        payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
        rows = self._decode_csv(payload)
        labels = {
            "Essence_E10": ("petrol_e10_eur_per_litre", None),
            "Diesel_B7": ("diesel_b7_eur_per_litre", None),
            "Hydrogene": ("hydrogen_eur_per_kg", None),
            "LPG": ("lpg_eur_per_litre", None),
            "Electricie_Fournisseur": ("home_electricity_eur_per_kwh", "Electricite_Fournisseur"),
            "Recharge_AC": ("public_charging_ac_eur_per_kwh", None),
            "Recharge_DC": ("public_charging_dc_eur_per_kwh", None),
        }
        prices = []
        for row in rows:
            month = (row.get("Intervalle") or "").strip()
            if not month:
                continue
            entry = {"month": month}
            for column, (name, alternate) in labels.items():
                entry[name] = _number(row.get(column) if column in row else row.get(alternate or column))
            prices.append(entry)
        prices.reverse()
        return {"count": len(prices[:months]), "note": "VAT included; prices are national averages published monthly",
                "prices": prices[:months], "source": url, "dataset": dataset.get("page")}

    def get_public_alerts(self, limit: int = 3, active_only: bool = True, language: str = "en") -> dict:
        limit = min(max(limit, 1), 10)
        language = language.lower()
        if language not in {"en", "fr", "de", "lb"}:
            raise ValueError("language must be one of en, fr, de, lb")
        dataset = self._cached(f"dataset:{ALERTS_SLUG}", 600, lambda: self.get_dataset(ALERTS_SLUG))
        resources = [item for item in dataset.get("resources", []) if item.get("format") == "xml" and item.get("url")]
        resources.sort(key=lambda item: item.get("last_modified") or "", reverse=True)
        now = datetime.now(timezone.utc)
        alerts = []
        for resource in resources[: limit * 3]:
            if len(alerts) >= limit:
                break
            payload, _ = self.http.get_bytes(resource["url"], allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            try:
                root = _parse_xml(payload)
            except ElementTree.ParseError:
                continue
            blocks = root.findall("{*}info")
            info = next((item for item in blocks if (item.findtext("{*}language") or "").lower().startswith(language)), None)
            if info is None and blocks:
                info = blocks[0]
            if info is None:
                continue
            expires = info.findtext("{*}expires")
            if active_only and not _alert_is_active(expires, now):
                continue
            alerts.append({
                "identifier": root.findtext("{*}identifier"),
                "sent": root.findtext("{*}sent"),
                "status": root.findtext("{*}status"),
                "message_type": root.findtext("{*}msgType"),
                "language": info.findtext("{*}language"),
                "category": info.findtext("{*}category"),
                "event": info.findtext("{*}event"),
                "urgency": info.findtext("{*}urgency"),
                "severity": info.findtext("{*}severity"),
                "certainty": info.findtext("{*}certainty"),
                "effective": info.findtext("{*}effective"),
                "expires": expires,
                "headline": info.findtext("{*}headline"),
                "description": _plain_text(info.findtext("{*}description")),
                "instruction": _plain_text(info.findtext("{*}instruction")),
                "areas": [area.findtext("{*}areaDesc") for area in info.findall("{*}area")],
                "source": resource["url"],
            })
        return {"count": len(alerts), "active_only": active_only, "alerts": alerts,
                "source": dataset.get("page") or f"{CATALOG}/datasets/{ALERTS_SLUG}/", "dataset": dataset.get("page")}

    def get_commune_leaders(self, commune: str | None = None) -> dict:
        dataset, resource = self._dataset_resource(COLLEGE_SLUG, format="csv")
        url = resource["url"]
        rows = self._cached(f"college:{url}", 3600, lambda: self._decode_csv(self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)[0]))
        needle = _fold(commune) if commune else None
        leaders = []
        for row in rows:
            # A filled end date means the mandate is over, so only blank ones are current.
            if (row.get("COAC_END_DATE") or "").strip():
                continue
            name = (row.get("COM_LABEL") or "").strip()
            if needle and needle not in _fold(name):
                continue
            leaders.append({
                "commune": name,
                "commune_code": row.get("COM_CODE"),
                "role": row.get("MAN_LABEL"),
                "first_name": row.get("ELU_FIRST_NAME"),
                "last_name": row.get("ELU_LAST_NAME"),
                "since": _iso_date(row.get("COAC_START_DATE")),
            })
        if needle and not leaders:
            raise ValueError(f"no current mandate matched commune: {commune}")
        leaders.sort(key=lambda item: (item["commune"], item["role"] != "Bourgmestre", item["last_name"] or ""))
        return {"count": len(leaders), "leaders": leaders, "source": url, "dataset": dataset.get("page")}

    def get_commune_population(self, commune: str | None = None) -> dict:
        dataset, resource = self._latest_resource(POPULATION_SLUG, "csv", ttl=3600)
        url = resource["url"]
        rows = self._cached(f"population:{url}", 3600, lambda: self._decode_csv(self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)[0]))
        needle = _fold(commune) if commune else None
        communes = []
        for row in rows:
            name = (row.get("COMMUNE_NOM") or "").strip()
            if not name or (needle and needle not in _fold(name)):
                continue
            counts = {key: int(_number(row.get(key)) or 0) for key in ("FEMMES_MINEURES", "HOMMES_MINEURS", "FEMMES_MAJEURES", "HOMMES_MAJEURS")}
            communes.append({
                "commune": name,
                "commune_code": row.get("COMMUNE_CODE"),
                "population": sum(counts.values()),
                "adults": counts["FEMMES_MAJEURES"] + counts["HOMMES_MAJEURS"],
                "minors": counts["FEMMES_MINEURES"] + counts["HOMMES_MINEURS"],
                "female": counts["FEMMES_MINEURES"] + counts["FEMMES_MAJEURES"],
                "male": counts["HOMMES_MINEURS"] + counts["HOMMES_MAJEURS"],
            })
        if needle and not communes:
            raise ValueError(f"unknown commune: {commune}")
        communes.sort(key=lambda item: -item["population"])
        return {"count": len(communes), "total_population": sum(item["population"] for item in communes),
                "as_of": resource.get("title"), "communes": communes, "source": url, "dataset": dataset.get("page")}

    def get_pharmacies_on_duty(self, date: str | None = None, locality: str | None = None) -> dict:
        if date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("date must be formatted as YYYY-MM-DD")
        rows = self._cached("pharmacies", 900, lambda: self._decode_csv(self.http.get_bytes(PHARMACY_ON_DUTY, {"Accept": "text/csv"})[0], delimiter=";"))
        available = sorted({(row.get("Date") or "").strip() for row in rows if row.get("Date")})
        wanted = date or datetime.now().date().isoformat()
        needle = _fold(locality) if locality else None
        pharmacies = []
        for row in rows:
            if (row.get("Date") or "").strip() != wanted:
                continue
            address = (row.get("Adresse") or "").strip()
            if needle and needle not in _fold(address):
                continue
            pharmacies.append({
                "name": (row.get("Pharmacie de Garde") or "").strip(),
                "address": address,
                "phone": (row.get("Téléphone") or "").strip() or None,
            })
        if not pharmacies and date and wanted not in available:
            raise ValueError(f"the on-duty feed only covers {available[0]} to {available[-1]}" if available else "the on-duty feed is empty")
        return {"date": wanted, "count": len(pharmacies), "covered_dates": available,
                "pharmacies": pharmacies, "source": PHARMACY_ON_DUTY}

    def get_electricity_prices(self) -> dict:
        dataset, resource = self._latest_resource(ELECTRICITY_SLUG, "xml", ttl=3600)
        url = resource["url"]

        def load() -> dict:
            payload, _ = self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)
            root = _parse_xml(payload)
            series = root.findall("{*}TimeSeries")
            if not series:
                raise UpstreamError("Day-ahead price document contained no time series")
            chosen = next(
                (item for item in series if (item.findtext("{*}classificationSequence_AttributeInstanceComponent.position") or "") == "1"),
                series[0],
            )
            period = chosen.find("{*}Period")
            start = _entsoe_time(period.find("{*}timeInterval").findtext("{*}start"))
            minutes = 15 if (period.findtext("{*}resolution") or "") == "PT15M" else 60
            # curveType A03: a point holds until the next one, so gaps repeat the previous price.
            by_position = {int(point.findtext("{*}position")): _number(point.findtext("{*}price.amount")) for point in period.findall("{*}Point")}
            if not by_position or start is None:
                raise UpstreamError("Day-ahead price document had no usable points")
            prices, last = [], None
            for position in range(1, max(by_position) + 1):
                last = by_position.get(position, last)
                if last is None:
                    continue
                prices.append({"start": (start + timedelta(minutes=minutes * (position - 1))).isoformat().replace("+00:00", "Z"),
                               "eur_per_mwh": last})
            return {"currency": chosen.findtext("{*}currency_Unit.name"), "unit": chosen.findtext("{*}price_Measure_Unit.name"),
                    "resolution_minutes": minutes, "prices": prices}

        data = self._cached(f"electricity:{url}", 3600, load)
        prices = data["prices"]
        cheapest = min(prices, key=lambda item: item["eur_per_mwh"])
        dearest = max(prices, key=lambda item: item["eur_per_mwh"])
        average = round(sum(item["eur_per_mwh"] for item in prices) / len(prices), 2)
        return {"day": resource.get("title"), "currency": data["currency"], "unit": data["unit"],
                "resolution_minutes": data["resolution_minutes"], "count": len(prices),
                "average_eur_per_mwh": average, "cheapest": cheapest, "most_expensive": dearest,
                "prices": prices, "source": url, "dataset": dataset.get("page")}

    def get_carsharing(self, query: str | None = None, fuel_type: str | None = None) -> dict:
        dataset, resource = self._dataset_resource(FLEX_SLUG, format="csv")
        url = resource["url"]
        # Refreshed every few minutes, and the versioned URL rotates with it.
        rows = self._cached(f"flex:{url}", 120, lambda: self._decode_csv(self.http.get_bytes(url, allowed_hosts=DATA_PUBLIC_RESOURCE_HOSTS)[0], delimiter=";"))
        needle = _fold(query) if query else None
        fuel_needle = _fold(fuel_type) if fuel_type else None
        vehicles = []
        for row in rows:
            haystack = _fold(" ".join(str(row.get(key) or "") for key in ("station_name", "station_town", "station_street", "station_zipcode", "brand_name", "model_name")))
            if needle and needle not in haystack:
                continue
            if fuel_needle and fuel_needle not in _fold(row.get("fuel_type") or ""):
                continue
            vehicles.append({
                "station": row.get("station_name"),
                "town": row.get("station_town"),
                "address": " ".join(part for part in ((row.get("station_street") or ""), (row.get("station_streetnumber") or "")) if part).strip() or None,
                "postcode": row.get("station_zipcode"),
                "vehicle": " ".join(part for part in ((row.get("brand_name") or ""), (row.get("model_name") or "")) if part).strip() or None,
                "car_type": row.get("car_type"),
                "fuel_type": row.get("fuel_type"),
                "latitude": _number(row.get("station_latitude")),
                "longitude": _number(row.get("station_longitude")),
            })
        vehicles.sort(key=lambda item: (item["town"] or "", item["station"] or ""))
        return {"count": len(vehicles), "note": "the CFL FLEX feed lists currently available vehicles only",
                "vehicles": vehicles, "source": url, "dataset": dataset.get("page")}

    def get_bike_sharing(self, query: str | None = None, available_only: bool = False) -> dict:
        def load() -> list[dict]:
            payload, _ = self.http.get_bytes(VELOK_STATIONS, {"Accept": "application/xml"})
            root = _parse_xml(payload)
            stations = []
            for node in root.iter("station"):
                values = {child.tag: (child.text or "").strip() for child in node}
                stations.append({
                    "station": values.get("nom"),
                    "locality": values.get("nomlocalite"),
                    "commune": values.get("nomcommune"),
                    "address": values.get("lieu") or None,
                    "latitude": _number(values.get("latitude")),
                    "longitude": _number(values.get("longitude")),
                    "bikes": int(_number(values.get("bikes")) or 0),
                    "ebikes": int(_number(values.get("ebikes")) or 0),
                    "free_docks": int(_number(values.get("libres")) or 0),
                    "in_maintenance": (values.get("maintenance") or "").lower() in ("1", "true"),
                })
            if not stations:
                raise UpstreamError("Vël'OK returned no stations")
            return stations

        stations = self._cached("velok", 120, load)
        needle = _fold(query) if query else None
        matches = []
        for station in stations:
            if needle and needle not in _fold(" ".join(str(station.get(key) or "") for key in ("station", "locality", "commune", "address"))):
                continue
            if available_only and station["bikes"] + station["ebikes"] == 0:
                continue
            matches.append(station)
        matches.sort(key=lambda item: (item["commune"] or "", item["station"] or ""))
        return {"count": len(matches), "total_bikes": sum(item["bikes"] + item["ebikes"] for item in matches),
                "stations": matches, "source": VELOK_STATIONS}

    def search_tenders(self, query: str | None = None, limit: int = 10, open_only: bool = True) -> dict:
        limit = min(max(limit, 1), 50)
        params: list[tuple[str, Any]] = [("itemsPerPage", limit), ("page", 1)]
        if open_only:
            params.append(("statutCalcule", 2))
        if query:
            if not query.strip():
                raise ValueError("query must not be empty")
            params.append(("search_full[]", query))
        url = f"{PMP_TENDERS}?{urlencode(params)}"
        # The API serves its HTML docs page unless JSON-LD is requested explicitly.
        data = self.http.get_json(url, headers={"Accept": "application/ld+json"})
        notices = []
        for item in data.get("hydra:member", []):
            notices.append({
                "reference": item.get("reference"),
                "title": item.get("intitule"),
                "buyer": item.get("directionServiceLibelle") or item.get("organismeDenomination"),
                "procedure": item.get("typeProcedureLibelle"),
                "nature": item.get("naturePrestationLibelle"),
                "published": item.get("dateMiseEnLigneCalcule"),
                "deadline": item.get("dateLimiteRemiseOffres"),
                "cpv_code": item.get("codeCpvPrincipal"),
                "estimated_value_eur": item.get("valeurEstimee") or None,
                "url": item.get("urlConsultation"),
            })
        return {"total": data.get("hydra:totalItems", len(notices)), "count": len(notices),
                "open_only": open_only, "tenders": notices, "source": url}


def _alert_is_active(expires: str | None, now: datetime) -> bool:
    if not expires:
        return True
    try:
        moment = datetime.fromisoformat(expires)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= now


def _plain_text(value: str | None) -> str | None:
    """CAP descriptions arrive as small HTML fragments; agents want the text."""
    if not value:
        return None
    text = re.sub(r"<br\s*/?>|</p>", "\n", value)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _iso_date(value: str | None) -> str | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _entsoe_time(value: str | None) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _number(value: str | None) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def _water_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def _read_bounded_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.flag_bits & 0x1:
        raise UpstreamError(f"Refusing encrypted ZIP member: {name}")
    if info.file_size > MAX_GTFS_MEMBER_BYTES:
        raise UpstreamError(f"ZIP member {name} exceeds {MAX_GTFS_MEMBER_BYTES} bytes")
    if info.file_size and info.compress_size == 0:
        raise UpstreamError(f"ZIP member {name} has an invalid compressed size")
    if (
        info.file_size > 1024 * 1024
        and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO
    ):
        raise UpstreamError(f"ZIP member {name} has a suspicious compression ratio")
    with archive.open(info) as member:
        payload = member.read(MAX_GTFS_MEMBER_BYTES + 1)
    if len(payload) > MAX_GTFS_MEMBER_BYTES:
        raise UpstreamError(f"ZIP member {name} exceeds {MAX_GTFS_MEMBER_BYTES} bytes")
    return payload
