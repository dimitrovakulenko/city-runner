"""Bounded public-data discovery; this interface deliberately accepts no user GPS."""

from __future__ import annotations

import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from backend.app.geography import MAX_OSM_XML_BYTES, MAX_OSM_XML_DEPTH, MAX_OSM_XML_ELEMENTS, _parse_osm_stream


WORLD_CATALOG_URL = "https://download.geofabrik.de/index-v1.json"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_CATALOG_FEATURES = 10_000
MAX_CITY_CANDIDATES = 50_000
MAX_POLYGON_POSITIONS = 500_000
NETWORK_TIMEOUT_SECONDS = 180
PROFILE_VERSION = "municipal-admin-v1"
QUERY_VERSION = "public-scope-v1"
# Municipal policies from the OSM administrative-level table; no global fallback.
DEFAULT_ADMIN_LEVELS = {"BE": [8], "DE": [8], "FR": [8], "NL": [8], "DK": [7], "NZ": [6]}


class CityGeographyError(ValueError):
    """Safe persisted code; never include upstream bodies, URLs or private data."""


def country_admin_levels() -> dict[str, list[int]]:
    result = {key: list(value) for key, value in DEFAULT_ADMIN_LEVELS.items()}
    configured = os.environ.get("CITY_ADMIN_LEVELS")
    if configured:
        try:
            values = json.loads(configured)
            if not isinstance(values, dict) or len(values) > 300:
                raise ValueError()
            for country, levels in values.items():
                _country(country)
                result[country] = _levels(levels)
        except (ValueError, TypeError, RecursionError) as error:
            raise CityGeographyError("invalid_city_country_policy") from error
    return result


def _country(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{2}", value) is None:
        raise CityGeographyError("invalid_city_country_policy")
    return value


def _levels(values) -> list[int]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 9:
        raise CityGeographyError("invalid_city_country_policy")
    if any(type(value) is not int or not 2 <= value <= 10 for value in values):
        raise CityGeographyError("invalid_city_country_policy")
    return sorted(set(values))


def _timestamp(value) -> str:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as error:
        raise CityGeographyError("invalid_city_provider_response") from error


def _position(value) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise CityGeographyError("invalid_city_provider_response")
    if any(type(number) not in (int, float) for number in value):
        raise CityGeographyError("invalid_city_provider_response")
    if not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90:
        raise CityGeographyError("invalid_city_provider_response")
    if any(not math.isfinite(number) for number in value):
        raise CityGeographyError("invalid_city_provider_response")
    return value


def _geometry(value, remaining: list[int]) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or value["type"] not in {"Polygon", "MultiPolygon"}:
        raise CityGeographyError("invalid_city_provider_response")
    polygons = [value.get("coordinates")] if value["type"] == "Polygon" else value.get("coordinates")
    if not isinstance(polygons, list) or not polygons:
        raise CityGeographyError("invalid_city_provider_response")
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise CityGeographyError("invalid_city_provider_response")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise CityGeographyError("invalid_city_provider_response")
            remaining[0] -= len(ring)
            if remaining[0] < 0:
                raise CityGeographyError("city_catalog_limit")
            for position in ring:
                _position(position)
            if ring[0] != ring[-1]:
                raise CityGeographyError("invalid_city_provider_response")
    return {"type": value["type"], "coordinates": value["coordinates"]}


def _json(payload: bytes) -> dict:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or "remark" in value or "error" in value:
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CityGeographyError("invalid_city_provider_response") from error


class CityGeographyProvider:
    def __init__(self, *, overpass_url: str = DEFAULT_OVERPASS_URL, transport=None):
        parsed = urlsplit(overpass_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise CityGeographyError("invalid_city_provider_configuration")
        self.overpass_url = overpass_url
        self.transport = transport

    def _fetch(self, url: str, limit: int, query: str | None = None) -> bytes:
        deadline = time.monotonic() + NETWORK_TIMEOUT_SECONDS
        try:
            with httpx.Client(timeout=httpx.Timeout(NETWORK_TIMEOUT_SECONDS, connect=10, read=15, write=15, pool=10), follow_redirects=False,
                              transport=self.transport, headers={"User-Agent": "CityRunner-development-geography/1.0", "Accept-Encoding": "identity"}) as client:
                with client.stream("POST" if query is not None else "GET", url,
                                   data={"data": query} if query is not None else None) as response:
                    if response.status_code == 429:
                        raise CityGeographyError("city_provider_rate_limited")
                    if response.status_code != 200:
                        raise CityGeographyError("city_provider_unavailable")
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise CityGeographyError("invalid_city_provider_response")
                    length = response.headers.get("content-length")
                    if length is not None and (len(length) > 20 or not length.isdigit() or int(length) > limit):
                        raise CityGeographyError("city_provider_response_limit")
                    chunks = bytearray()
                    for chunk in response.iter_raw(chunk_size=64 * 1024):
                        if time.monotonic() > deadline:
                            raise CityGeographyError("city_provider_timeout")
                        if len(chunks) + len(chunk) > limit:
                            raise CityGeographyError("city_provider_response_limit")
                        chunks.extend(chunk)
                    return bytes(chunks)
        except httpx.TimeoutException as error:
            raise CityGeographyError("city_provider_timeout") from error
        except httpx.HTTPError as error:
            raise CityGeographyError("city_provider_unavailable") from error

    def fetch_world_catalog(self) -> dict:
        value = _json(self._fetch(WORLD_CATALOG_URL, MAX_CATALOG_BYTES))
        features = value.get("features")
        if value.get("type") != "FeatureCollection" or not isinstance(features, list) or not 1 <= len(features) <= MAX_CATALOG_FEATURES:
            raise CityGeographyError("invalid_city_provider_response")
        regions = []
        remaining = [MAX_POLYGON_POSITIONS]
        for feature in features:
            if not isinstance(feature, dict) or feature.get("type") != "Feature" or not isinstance(feature.get("properties"), dict):
                raise CityGeographyError("invalid_city_provider_response")
            countries = feature["properties"].get("iso3166-1:alpha2", [])
            if not isinstance(countries, list) or len(countries) > 300:
                raise CityGeographyError("invalid_city_provider_response")
            countries = [_country(country) for country in countries]
            if len(set(countries)) != len(countries):
                raise CityGeographyError("invalid_city_provider_response")
            if len(regions) + len(countries) > MAX_CATALOG_FEATURES:
                raise CityGeographyError("city_catalog_limit")
            # All physical geometry counts against the bound, even non-country extracts.
            geometry = _geometry(feature.get("geometry"), remaining)
            for country in countries:
                regions.append({"country_code": _country(country), "geometry": geometry})
        if not regions:
            raise CityGeographyError("invalid_city_provider_response")
        return {"regions": regions}

    def fetch_country_catalog(self, country: str, levels: list[int]) -> dict:
        country = _country(country)
        levels = _levels(levels)
        level_filter = "|".join(str(value) for value in levels)
        query = (f'[out:json][timeout:120][maxsize:134217728];area["ISO3166-1"="{country}"]["admin_level"="2"]->.country;'
                 f'.country out ids;rel(area.country)["boundary"="administrative"]["admin_level"~"^({level_filter})$"];out tags bb;')
        value = _json(self._fetch(self.overpass_url, MAX_CATALOG_BYTES, query))
        metadata = value.get("osm3s")
        if not isinstance(metadata, dict):
            raise CityGeographyError("invalid_city_provider_response")
        timestamp = _timestamp(metadata.get("timestamp_osm_base"))
        elements = value.get("elements")
        if not isinstance(elements, list) or not 1 <= len(elements) <= MAX_CITY_CANDIDATES + 1:
            raise CityGeographyError("city_catalog_limit")
        areas = [element for element in elements if isinstance(element, dict) and element.get("type") == "area"]
        if len(areas) != 1:
            raise CityGeographyError("city_country_boundary_unavailable")
        cities = []
        seen = set()
        for element in elements:
            if element is areas[0]:
                continue
            if not isinstance(element, dict) or element.get("type") != "relation":
                raise CityGeographyError("invalid_city_provider_response")
            identifier = element.get("id")
            tags = element.get("tags")
            bounds = element.get("bounds")
            if type(identifier) is not int or not 1 <= identifier <= 9_223_372_036_854_775_807 or identifier in seen:
                raise CityGeographyError("invalid_city_provider_response")
            if not isinstance(tags, dict) or tags.get("boundary") != "administrative" or not isinstance(tags.get("admin_level"), str) or tags["admin_level"] not in {str(level) for level in levels}:
                raise CityGeographyError("invalid_city_provider_response")
            name = tags.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 200 or not isinstance(bounds, dict):
                raise CityGeographyError("invalid_city_provider_response")
            west, south = _position([bounds.get("minlon"), bounds.get("minlat")])
            east, north = _position([bounds.get("maxlon"), bounds.get("maxlat")])
            if west > east or south > north:
                raise CityGeographyError("invalid_city_provider_response")
            cities.append({"relation_id": identifier, "name": name, "bounds": [west, south, east, north]})
            seen.add(identifier)
        if not cities:
            raise CityGeographyError("city_country_boundary_unavailable")
        return {"source_timestamp": timestamp, "cities": cities}

    def fetch_city_extract(self, relation_id: int) -> dict:
        if type(relation_id) is not int or not 1 <= relation_id <= 9_223_372_036_854_775_807:
            raise CityGeographyError("invalid_city_relation")
        query = (f'[out:xml][timeout:120][maxsize:268435456];rel({relation_id})->.city;.city map_to_area->.area;'
                 '.area out ids;(.city;way(r.city);way(area.area)["highway"]["name"];);out body;>;out skel qt;')
        payload = self._fetch(self.overpass_url, MAX_OSM_XML_BYTES, query)
        # Inspect the response without building a second full XML tree. The importer
        # remains responsible for complete boundary/street/coordinate validation.
        area_ids = []
        try:
            depth = count = 0
            root = None
            for event, element in ElementTree.iterparse(io.BytesIO(payload), events=("start", "end")):
                if event == "start":
                    depth += 1
                    count += 1
                    if depth > MAX_OSM_XML_DEPTH or count > MAX_OSM_XML_ELEMENTS:
                        raise CityGeographyError("city_provider_response_limit")
                    if depth == 1:
                        root = element
                    continue
                if element.tag == "remark" or element.tag == "error":
                    raise CityGeographyError("invalid_city_provider_response")
                if element.tag == "area":
                    if depth != 2:
                        raise CityGeographyError("city_boundary_unavailable")
                    area_ids.append(element.get("id"))
                element.clear()
                if depth == 2 and root is not None:
                    root.remove(element)
                depth -= 1
            if area_ids != [str(3_600_000_000 + relation_id)]:
                raise CityGeographyError("city_boundary_unavailable")
            document = _parse_osm_stream(io.BytesIO(payload), {relation_id})
            relation = document.relations.get(relation_id)
            if relation is None or relation.tags.get("boundary") != "administrative":
                raise CityGeographyError("city_boundary_unavailable")
            timestamp = _timestamp(document.source_timestamp)
        except CityGeographyError:
            raise
        except (ValueError, ElementTree.ParseError, DefusedXmlException) as error:
            raise CityGeographyError("invalid_city_provider_response") from error
        return {"source_timestamp": timestamp, "xml": payload}


def get_city_geography_provider() -> CityGeographyProvider | None:
    name = os.environ.get("CITY_GEOGRAPHY_PROVIDER", "disabled")
    if name == "disabled":
        return None
    if name != "overpass":
        raise CityGeographyError("invalid_city_provider_configuration")
    return CityGeographyProvider(overpass_url=os.environ.get("CITY_OVERPASS_URL", DEFAULT_OVERPASS_URL))
