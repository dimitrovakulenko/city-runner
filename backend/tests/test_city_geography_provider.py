import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from backend.app.city_geography_provider import (
    CityGeographyError, CityGeographyProvider, WORLD_CATALOG_URL,
    country_admin_levels, get_city_geography_provider,
)


COUNTRY = {
    "osm3s": {"timestamp_osm_base": "2026-10-13T00:00:00Z"},
    "elements": [
        {"type": "area", "id": 3600000001},
        {"type": "relation", "id": 42,
         "tags": {"name": "Antwerp", "boundary": "administrative", "admin_level": "8"},
         "bounds": {"minlon": 4., "minlat": 51., "maxlon": 4.5, "maxlat": 51.5}},
    ],
}
WORLD = {"type": "FeatureCollection", "features": [{
    "type": "Feature", "properties": {"iso3166-1:alpha2": ["BE", "NL"]},
    "geometry": {"type": "MultiPolygon", "coordinates": [[
        [[3., 50.], [6., 50.], [6., 52.], [3., 50.]],
        [[4., 51.], [4.1, 51.], [4.1, 51.1], [4., 51.]],
    ]]},
}]}
CITY = b'''<osm version="0.6"><meta osm_base="2026-10-13T00:00:00Z"/>
<area id="3600000042"/>
<node id="1" lon="4" lat="51"/><node id="2" lon="4.5" lat="51"/>
<node id="3" lon="4.5" lat="51.5"/><node id="4" lon="4" lat="51.5"/>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
<way id="11"><nd ref="1"/><nd ref="2"/><tag k="name" v="Test Street"/><tag k="highway" v="residential"/></way>
<relation id="42"><member type="way" ref="10" role="outer"/>
<tag k="boundary" v="administrative"/><tag k="admin_level" v="8"/><tag k="name" v="Antwerp"/>
</relation></osm>'''


class CityGeographyProviderTests(unittest.TestCase):
    def provider(self, response, requests=None):
        def transport(request):
            if requests is not None:
                requests.append(request)
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        return CityGeographyProvider(transport=httpx.MockTransport(transport))

    def test_country_query_contains_only_fixed_public_scope_and_returns_original_public_bounds(self):
        requests = []
        result = self.provider(httpx.Response(200, json=COUNTRY), requests).fetch_country_catalog("BE", [8])
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn('["ISO3166-1"="BE"]', query)
        self.assertIn('"^(8)$"', query)
        self.assertNotIn("51.", query)
        self.assertNotIn("bbox", query)
        self.assertEqual(result["cities"], [{"relation_id": 42, "name": "Antwerp", "bounds": [4., 51., 4.5, 51.5]}])
        self.assertEqual(result["source_timestamp"], "2026-10-13T00:00:00+00:00")

    def test_world_catalog_preserves_holes_and_multi_country_candidates_without_following_supplied_urls(self):
        requests = []
        world = json.loads(json.dumps(WORLD))
        world["features"][0]["properties"]["urls"] = {"pbf": "http://private.invalid/"}
        result = self.provider(httpx.Response(200, json=world), requests).fetch_world_catalog()
        self.assertEqual([row["country_code"] for row in result["regions"]], ["BE", "NL"])
        self.assertEqual(result["regions"][0]["geometry"], WORLD["features"][0]["geometry"])
        self.assertEqual([str(request.url) for request in requests], [WORLD_CATALOG_URL])

    def test_city_query_uses_only_relation_id_and_retains_exact_snapshot_bytes(self):
        requests = []
        result = self.provider(httpx.Response(200, content=CITY), requests).fetch_city_extract(42)
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn("rel(42)", query)
        self.assertIn('way(area.area)["highway"]["name"]', query)
        self.assertEqual(result["xml"], CITY)

    def test_partial_city_area_and_xml_errors_are_safe(self):
        for payload in (
            CITY.replace(b'<area id="3600000042"/>', b''),
            CITY.replace(b'3600000042', b'3600000099'),
            CITY.replace(b'<area id="3600000042"/>', b'').replace(b'<way id="10">', b'<way id="10"><area id="3600000042"/>'),
            CITY.replace(b'</osm>', b'<remark>runtime error with upstream path</remark></osm>'),
            CITY[:-9],
            b'<!DOCTYPE osm [<!ENTITY x "foo">]><osm>&x;</osm>',
        ):
            with self.subTest(payload=payload[:20]), self.assertRaises(CityGeographyError) as caught:
                self.provider(httpx.Response(200, content=payload)).fetch_city_extract(42)
            self.assertNotIn("upstream", str(caught.exception))

    def test_city_xml_physical_depth_and_element_bounds_precede_full_parsing(self):
        for constant, value in (("MAX_OSM_XML_DEPTH", 2), ("MAX_OSM_XML_ELEMENTS", 5)):
            with self.subTest(constant=constant), patch("backend.app.city_geography_provider." + constant, value):
                with self.assertRaisesRegex(CityGeographyError, "city_provider_response_limit"):
                    self.provider(httpx.Response(200, content=CITY)).fetch_city_extract(42)

    def test_country_partial_missing_scope_and_duplicate_relations_are_rejected(self):
        values = []
        for modify in (
            lambda value: value.update(remark="runtime error"),
            lambda value: value.update(osm3s="invalid metadata"),
            lambda value: value["elements"].pop(0),
            lambda value: value["elements"].append(value["elements"][1]),
            lambda value: value["elements"][1]["tags"].update(admin_level="7"),
            lambda value: value["elements"][1]["bounds"].update(minlon=8),
        ):
            value = json.loads(json.dumps(COUNTRY)); modify(value); values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(CityGeographyError):
                self.provider(httpx.Response(200, json=value)).fetch_country_catalog("BE", [8])

    def test_catalog_rejects_duplicate_json_keys_and_nonfinite_coordinates(self):
        with self.assertRaises(CityGeographyError):
            self.provider(httpx.Response(200, content=b'{"type":"FeatureCollection","features":[],"features":[]}')).fetch_world_catalog()
        world = json.loads(json.dumps(WORLD))
        world["features"][0]["geometry"]["coordinates"][0][0][1][0] = float("nan")
        with self.assertRaises(CityGeographyError):
            self.provider(httpx.Response(200, content=json.dumps(world).encode())).fetch_world_catalog()

    def test_invalid_public_identifiers_never_reach_network(self):
        requests = []
        provider = self.provider(httpx.Response(200), requests)
        for country, levels in (("BE\"]bad", [8]), ("be", [8]), ("BE", [True]), ("BE", []), ("BE", [11])):
            with self.subTest(country=country, levels=levels), self.assertRaises(CityGeographyError):
                provider.fetch_country_catalog(country, levels)
        for identifier in (True, 0, -1, 2**63, "42"):
            with self.subTest(identifier=identifier), self.assertRaises(CityGeographyError):
                provider.fetch_city_extract(identifier)
        self.assertEqual(requests, [])

    def test_catalog_expansion_and_duplicate_country_codes_are_bounded(self):
        world = json.loads(json.dumps(WORLD))
        with patch("backend.app.city_geography_provider.MAX_CATALOG_FEATURES", 1):
            with self.assertRaisesRegex(CityGeographyError, "city_catalog_limit"):
                self.provider(httpx.Response(200, json=world)).fetch_world_catalog()
        world["features"][0]["properties"]["iso3166-1:alpha2"] = ["BE", "BE"]
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            self.provider(httpx.Response(200, json=world)).fetch_world_catalog()

    def test_http_errors_redirects_and_response_bounds_are_safe(self):
        for response, code in (
            (httpx.Response(429), "city_provider_rate_limited"),
            (httpx.Response(302, headers={"location": "http://private.invalid"}), "city_provider_unavailable"),
            (httpx.Response(503, content=b"secret upstream message"), "city_provider_unavailable"),
            (httpx.Response(200, headers={"content-length": "99999999"}), "city_provider_response_limit"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(CityGeographyError, code):
                self.provider(response).fetch_world_catalog()
        with patch("backend.app.city_geography_provider.MAX_CATALOG_BYTES", 10):
            with self.assertRaisesRegex(CityGeographyError, "city_provider_response_limit"):
                self.provider(httpx.Response(200, content=b"x" * 11)).fetch_world_catalog()

    def test_timeout_and_network_exceptions_do_not_expose_details(self):
        for error, code in ((httpx.ReadTimeout("private endpoint"), "city_provider_timeout"),
                            (httpx.ConnectError("private endpoint"), "city_provider_unavailable")):
            def fail(request):
                raise error
            provider = CityGeographyProvider(transport=httpx.MockTransport(fail))
            with self.subTest(code=code), self.assertRaisesRegex(CityGeographyError, code):
                provider.fetch_world_catalog()
        with patch("backend.app.city_geography_provider.time.monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(CityGeographyError, "city_provider_timeout"):
                self.provider(httpx.Response(200, content=b"{}")).fetch_world_catalog()

    def test_encoded_responses_are_rejected_before_any_decompression(self):
        import gzip
        requests = []
        body = gzip.compress(b"x" * 1_000_000)
        # Read the synthetic wire bytes without decoding them for the transport fixture.
        response = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(body))
        provider = CityGeographyProvider(transport=httpx.MockTransport(lambda request: (
            requests.append(request) or response)))
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            provider.fetch_world_catalog()
        self.assertEqual(requests[0].headers["accept-encoding"], "identity")
        self.assertFalse(response.is_stream_consumed)

    def test_malformed_field_types_and_huge_numbers_return_safe_codes(self):
        for mutate in (
            lambda value: value["features"][0]["geometry"].update(type=[]),
            lambda value: value["features"][0]["geometry"]["coordinates"][0][0][1].__setitem__(0, 10**1000),
        ):
            value = json.loads(json.dumps(WORLD)); mutate(value)
            with self.subTest(value=str(value)[:80]), self.assertRaises(CityGeographyError):
                self.provider(httpx.Response(200, content=json.dumps(value).encode())).fetch_world_catalog()
        value = json.loads(json.dumps(COUNTRY))
        value["elements"][1]["tags"]["admin_level"] = []
        with self.assertRaises(CityGeographyError):
            self.provider(httpx.Response(200, json=value)).fetch_country_catalog("BE", [8])

    def test_policy_is_explicit_and_trusted_overrides_are_validated(self):
        with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": '{"US":[8,6],"DK":[7]}'}, clear=True):
            policies = country_admin_levels()
        self.assertEqual(policies["US"], [6, 8])
        self.assertEqual(policies["DK"], [7])
        self.assertNotIn("ZZ", policies)
        for value in ('{"ZZ":[true]}', '[]', '{"ZZ":[]}'):
            with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": value}, clear=True):
                with self.assertRaisesRegex(CityGeographyError, "invalid_city_country_policy"):
                    country_admin_levels()

    def test_disabled_mode_and_endpoint_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_city_geography_provider())
        with patch.dict(os.environ, {"CITY_GEOGRAPHY_PROVIDER": "overpass"}, clear=True):
            self.assertIsInstance(get_city_geography_provider(), CityGeographyProvider)
        for endpoint in ("http://example.com", "https://user:secret@example.com", "https://example.com/#fragment", "https://example.com/?query"):
            with self.subTest(endpoint=endpoint), self.assertRaises(CityGeographyError):
                CityGeographyProvider(overpass_url=endpoint)


if __name__ == "__main__":
    unittest.main()
