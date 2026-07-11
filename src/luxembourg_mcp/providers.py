"""Adapters for official Luxembourg public-data services."""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
import zipfile
from datetime import datetime
from xml.etree import ElementTree
from typing import Any
from urllib.parse import quote, urlencode

from .http import HttpClient, UpstreamError

CATALOG = "https://data.public.lu/api/1"
GEOCODE = "https://apiv3.geoportail.lu/geocode"
FEATURES = "https://features.geoportail.lu"
WEATHER_ALERTS_SLUG = "meteolux-weather-warnings-for-the-grand-duchy-of-luxembourg-1"
LEGILUX = "https://data.legilux.public.lu/sparqlendpoint"
STATEC = "https://lustat.statec.lu/rest"
VDL_PARKING = "https://feed.vdl.lu/circulation/parking/feed.json"
CFL_PARKING = "https://pr-mobile-a.cfl.lu/OpenData/ParkAndRide"
CITA_TRAFFIC = "https://www.cita.lu/info_trafic/datex"
WATER_LEVELS = "https://inondations.lu/water-level-export-by-time/all?localtime"
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


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _xlsx_sheet_rows(payload: bytes, sheet_name: str | None) -> tuple[str, list[str], list[dict[str, str]]]:
    """Minimal stdlib XLSX reader: returns (chosen sheet, all sheet names, rows as {column: value})."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            workbook = ElementTree.fromstring(_read_bounded_zip_member(archive, "xl/workbook.xml"))
            rels = {
                rel.get("Id"): rel.get("Target")
                for rel in ElementTree.fromstring(_read_bounded_zip_member(archive, "xl/_rels/workbook.xml.rels"))
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
                strings = ElementTree.fromstring(_read_bounded_zip_member(archive, "xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")) for si in strings]
            sheet = ElementTree.fromstring(_read_bounded_zip_member(archive, path))
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
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, ttl: int, loader: Any) -> Any:
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        value = loader()
        if key not in self._cache and len(self._cache) >= 32:
            oldest = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest)
        self._cache[key] = (time.monotonic(), value)
        return value

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
                root = ElementTree.fromstring(payload)
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
            root = ElementTree.fromstring(payload)
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
            row for row in csv.reader(io.StringIO(payload.decode("utf-8-sig", errors="replace")), delimiter=";")
            if any(cell.strip() for cell in row)
        ]
        headers = {row[0].strip().casefold(): row for row in rows if row and row[0].strip().casefold() in {"name", "number", "unit"}}
        measurements = [row for row in rows if row and _water_timestamp(row[0]) is not None]
        if not {"name", "number", "unit"}.issubset(headers) or not measurements:
            raise UpstreamError("Water-level export contained no measurements")
        names, numbers, units = headers["name"], headers["number"], headers["unit"]
        latest = max(measurements, key=lambda row: _water_timestamp(row[0]))
        results = []
        for index in range(1, min(len(names), len(latest))):
            item = {
                "name": names[index],
                "station_number": numbers[index] if index < len(numbers) else None,
                "unit": units[index] if index < len(units) else None,
                "value": _number(latest[index]),
            }
            if not station or _fold(station) in _fold(item["name"]):
                results.append(item)
        return {"measured_at": latest[0], "count": len(results), "stations": results, "source": WATER_LEVELS}

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
                root = ElementTree.fromstring(payload)
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
                root = ElementTree.fromstring(payload)
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

    def get_city_mobility(self, category: str) -> dict:
        layer = VDL_MOBILITY_LAYERS.get(category)
        if layer is None:
            raise ValueError(f"category must be one of {', '.join(VDL_MOBILITY_LAYERS)}")
        params = {"where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson"}
        url = f"https://maps.vdl.lu/arcgis/rest/services/OPENDATA/GEOJSON/FeatureServer/{layer}/query?{urlencode(params)}"
        data = self.http.get_json(url)
        return {"category": category, "count": len(data.get("features", [])), "features": data.get("features", []), "source": url}


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
