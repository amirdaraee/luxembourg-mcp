"""Adapters for official Luxembourg public-data services."""

from __future__ import annotations

import csv
import io
import json
import re
import time
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
MAX_GTFS_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100


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
            needle = query.casefold()
            collections = [
                item for item in collections
                if needle in " ".join(
                    str(value) for value in (
                        item.get("title", ""),
                        item.get("description", ""),
                        *(item.get("keywords") or []),
                    )
                ).casefold()
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
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        alerts = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        return {
            "language": language,
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
        needle = query.casefold()
        matches = [
            item for item in self._statec_dataflows()
            if needle in " ".join(str(value or "") for value in item.values()).casefold()
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
            needle = query.casefold()
            parking = [item for item in parking if needle in json.dumps(item, ensure_ascii=False).casefold()]
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
            if not station or station.casefold() in item["name"].casefold():
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
            needle = city.casefold()
            stations = [item for item in stations if needle in (item.get("adr_city") or "").casefold()]
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
        needle = query.casefold()
        results = [row for row in rows if needle in " ".join(row.values()).casefold()][: min(max(limit, 1), 100)]
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
        needle = query.casefold()
        matches = [stop for stop in stops if needle in " ".join(stop.values()).casefold()][: min(max(limit, 1), 100)]
        return {"count": len(matches), "stops": matches, "source": url, "dataset": dataset.get("page")}

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
