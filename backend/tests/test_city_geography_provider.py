import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from backend.app.city_geography_provider import (
    CityGeographyError, CityGeographyProvider, WORLD_CATALOG_URL,
    country_admin_levels, country_selector_profile, get_city_geography_provider,
)
from backend.app.municipal_rules import (
    AZ_PROFILE, CA_PROVINCES, DEFAULT_PROFILE, profile_is_valid, relation_matches, validate_selection,
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


def canada_catalog(relation_scopes, relations):
    elements = [{"type": "area", "id": 3600000001}]
    by_id = {relation["id"]: relation for relation in relations}
    for index, scope in enumerate(CA_PROVINCES, start=1):
        province_relation_id = 1000 + index
        province_area_id = 3_600_000_000 + province_relation_id
        elements.append({"type": "relation", "id": province_relation_id,
            "tags": {"ISO3166-2": scope, "boundary": "administrative", "admin_level": "4"}})
        elements.append({"type": "area", "id": province_area_id})
        elements.extend(json.loads(json.dumps(by_id[value])) for value in relation_scopes.get(scope, []))
        elements.append({"type": "scope", "id": index})
    return {"osm3s": COUNTRY["osm3s"], "elements": elements}


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
            lambda value: value["elements"][1]["tags"].__setitem__("ref:gss", []),
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

    def test_unknown_or_inconsistent_selector_profiles_fail_before_network(self):
        requests = []
        provider = self.provider(httpx.Response(200), requests)
        for country, levels, profile in (
            ("GB", [6, 8], "future-gb-profile"),
            ("CA", [6, 8], "gb-local-authority-v1"),
        ):
            with self.subTest(country=country, profile=profile), self.assertRaises(CityGeographyError):
                provider.fetch_country_catalog(country, levels, selector_profile=profile)
        with self.assertRaises(CityGeographyError):
            provider.fetch_city_extract(42, selector_profile="ca-municipal-v1", country_code="CA", admin_levels=[6, 8])
        self.assertEqual(requests, [])

    def test_catalog_expansion_and_duplicate_country_codes_are_bounded(self):
        world = json.loads(json.dumps(WORLD))
        with patch("backend.app.city_geography_provider.MAX_CATALOG_FEATURES", 1):
            with self.assertRaisesRegex(CityGeographyError, "city_catalog_limit"):
                self.provider(httpx.Response(200, json=world)).fetch_world_catalog()
        world["features"][0]["properties"]["iso3166-1:alpha2"] = ["BE", "BE"]
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            self.provider(httpx.Response(200, json=world)).fetch_world_catalog()
        world = json.loads(json.dumps(WORLD))
        with patch("backend.app.city_geography_provider.MAX_POLYGON_POSITIONS", 12):
            # Eight physical positions expand to sixteen for the two country scopes.
            with self.assertRaisesRegex(CityGeographyError, "city_catalog_limit"):
                self.provider(httpx.Response(200, json=world)).fetch_world_catalog()
        body = json.dumps(world, separators=(',', ':')).encode()
        with patch("backend.app.city_geography_provider.MAX_CATALOG_BYTES", len(body) + 1):
            # The normalized result duplicates country geometry; bound that too.
            with self.assertRaisesRegex(CityGeographyError, "city_catalog_limit"):
                self.provider(httpx.Response(200, content=body)).fetch_world_catalog()

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
        with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": '{"US":[8,4],"DK":[7]}'}, clear=True):
            policies = country_admin_levels()
        self.assertEqual(policies["US"], [4, 8])
        self.assertEqual(policies["DK"], [7])
        self.assertNotIn("ZZ", policies)
        for value in ('{"ZZ":[true]}', '[]', '{"ZZ":[]}'):
            with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": value}, clear=True):
                with self.assertRaisesRegex(CityGeographyError, "invalid_city_country_policy"):
                    country_admin_levels()

    def test_country_policies_emit_exact_municipal_level_and_reject_adjacent_levels(self):
        expected = {
            "AT": 8, "BE": 8, "BJ": 6, "BR": 8, "CH": 8, "CL": 8, "CO": 6, "CZ": 8,
            "DE": 8, "DK": 7, "EE": 7, "ES": 8, "FI": 8, "FR": 8, "GR": 7,
            "HU": 8, "IS": 6, "IT": 8, "JP": 7, "LU": 8, "LV": 5, "MX": 6, "NL": 8,
            "NO": 7, "NZ": 6, "PE": 8, "PH": 6, "PL": 7, "PT": 7, "RO": 8,
            "SE": 7, "SI": 8, "SK": 8,
        }
        with patch.dict(os.environ, {}, clear=True):
            policies = country_admin_levels()
        self.assertEqual({country: values[0] for country, values in policies.items()
                          if country not in {"AZ", "CA", "GB", "US"}}, expected)
        self.assertEqual(len(policies), 37)
        self.assertEqual(policies["AZ"], [8])
        self.assertEqual(country_selector_profile("AZ"), AZ_PROFILE)
        for country, level in expected.items():
            with self.subTest(country=country):
                self.assertEqual(country_selector_profile(country), "admin-levels-v1")
                response = json.loads(json.dumps(COUNTRY))
                response["elements"][1]["tags"]["admin_level"] = str(level)
                requests = []
                result = self.provider(httpx.Response(200, json=response), requests).fetch_country_catalog(country, [level])
                query = parse_qs(requests[0].content.decode())["data"][0]
                self.assertIn(f'["ISO3166-1"="{country}"]', query)
                self.assertIn(f'"^({level})$"', query)
                self.assertEqual(len(result["cities"]), 1)

                for adjacent in (level - 1, level + 1):
                    response["elements"][1]["tags"]["admin_level"] = str(adjacent)
                    with self.subTest(adjacent=adjacent), self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
                        self.provider(httpx.Response(200, json=response)).fetch_country_catalog(country, [level])

    def test_azerbaijan_profile_requires_positive_municipal_proof(self):
        self.assertTrue(profile_is_valid("AZ", AZ_PROFILE))
        self.assertTrue(profile_is_valid("AZ", DEFAULT_PROFILE))  # queued legacy snapshots retain old semantics
        self.assertFalse(profile_is_valid("BJ", AZ_PROFILE))
        self.assertTrue(validate_selection("AZ", AZ_PROFILE, [8], None))
        self.assertFalse(validate_selection("AZ", AZ_PROFILE, [6], None))
        self.assertFalse(validate_selection("AZ", AZ_PROFILE, [8], "AZ-BA"))

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(country_admin_levels()["AZ"], [8])
        with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": '{"AZ":[8]}'}, clear=True):
            self.assertEqual(country_admin_levels()["AZ"], [8])
        for override in ('{"AZ":[6]}', '{"AZ":[8,10]}'):
            with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": override}, clear=True):
                with self.subTest(override=override), self.assertRaisesRegex(
                    CityGeographyError, "invalid_city_country_policy"):
                    country_admin_levels()

        payload = {"osm3s": COUNTRY["osm3s"], "elements": [COUNTRY["elements"][0],
            {"type": "relation", "id": 3912206,
             "tags": {"name": "Sumqayit", "boundary": "administrative", "admin_level": "8", "place": "city"},
             "bounds": COUNTRY["elements"][1]["bounds"]},
            {"type": "relation", "id": 90001,
             "tags": {"name": "Baku rayonu", "boundary": "administrative", "admin_level": "8"},
             "bounds": COUNTRY["elements"][1]["bounds"]},
            {"type": "relation", "id": 90002,
             "tags": {"name": "Ganja rayonu", "boundary": "administrative", "admin_level": "8"},
             "bounds": COUNTRY["elements"][1]["bounds"]}]}
        requests = []
        result = self.provider(httpx.Response(200, json=payload), requests).fetch_country_catalog(
            "AZ", [8], selector_profile=AZ_PROFILE)
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn('["ISO3166-1"="AZ"]', query)
        self.assertIn('["admin_level"="8"]', query)
        self.assertIn('["place"~"^(city|town|village|municipality)$"]', query)
        self.assertEqual([city["relation_id"] for city in result["cities"]], [3912206])
        wrong_level = json.loads(json.dumps(payload))
        wrong_level["elements"][1]["tags"]["admin_level"] = "6"
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            self.provider(httpx.Response(200, json=wrong_level)).fetch_country_catalog(
                "AZ", [8], selector_profile=AZ_PROFILE)

        for key, value in (("place", "county"), ("border_type", "district"), ("designation", "district")):
            invalid = json.loads(json.dumps(payload))
            invalid["elements"][1]["tags"][key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(CityGeographyError, "city_country_boundary_unavailable"):
                    self.provider(httpx.Response(200, json=invalid)).fetch_country_catalog(
                        "AZ", [8], selector_profile=AZ_PROFILE)
        malformed = json.loads(json.dumps(payload))
        malformed["elements"][1]["tags"]["place"] = ["city"]
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            self.provider(httpx.Response(200, json=malformed)).fetch_country_catalog(
                "AZ", [8], selector_profile=AZ_PROFILE)
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_selector"):
            self.provider(httpx.Response(200, json=payload)).fetch_country_catalog(
                "AZ", [8], selector_profile="unknown-profile")

        accepted_tags = {"boundary": "administrative", "admin_level": "8", "place": "city"}
        self.assertTrue(relation_matches("AZ", AZ_PROFILE, [8], accepted_tags))
        self.assertFalse(relation_matches("AZ", AZ_PROFILE, [6, 8], accepted_tags))
        self.assertFalse(relation_matches("AZ", AZ_PROFILE, [8], accepted_tags, selection_scope="AZ-BA"))
        self.assertFalse(relation_matches("AZ", "unknown-profile", [8], accepted_tags))
        for key, value in (("place", "district"), ("border_type", "county"), ("designation", "district")):
            self.assertFalse(relation_matches("AZ", AZ_PROFILE, [8], {**accepted_tags, key: value}))

        # Old queued AZ tasks stay on the numeric-only selector even after the new default is enabled.
        legacy = json.loads(json.dumps(payload))
        legacy["elements"][1]["tags"].pop("place")
        legacy_requests = []
        old_result = self.provider(httpx.Response(200, json=legacy), legacy_requests).fetch_country_catalog(
            "AZ", [8], selector_profile=DEFAULT_PROFILE)
        old_query = parse_qs(legacy_requests[0].content.decode())["data"][0]
        self.assertNotIn('["place"~', old_query)
        self.assertIn(3912206, {city["relation_id"] for city in old_result["cities"]})

    def test_azerbaijan_city_extract_revalidates_captured_selector_and_preserves_legacy_snapshot(self):
        relation_id = 3912206
        area_id = 3_600_000_000 + relation_id
        accepted = CITY.replace(b"3600000042", str(area_id).encode()).replace(
            b'<relation id="42">', f'<relation id="{relation_id}">'.encode()).replace(
            b'<tag k="admin_level" v="8"/>',
            b'<tag k="admin_level" v="8"/><tag k="place" v="city"/>')
        result = self.provider(httpx.Response(200, content=accepted)).fetch_city_extract(
            relation_id, selector_profile=AZ_PROFILE, country_code="AZ", admin_levels=[8])
        self.assertEqual(result["xml"], accepted)

        for mutate in (
            lambda data: data.replace(b'<tag k="place" v="city"/>', b''),
            lambda data: data.replace(b'<tag k="place" v="city"/>', b'<tag k="place" v="county"/>'),
            lambda data: data.replace(b'<tag k="place" v="city"/>',
                                      b'<tag k="place" v="city"/><tag k="designation" v="district"/>'),
        ):
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                CityGeographyError, "city_boundary_unavailable"):
                self.provider(httpx.Response(200, content=mutate(accepted))).fetch_city_extract(
                    relation_id, selector_profile=AZ_PROFILE, country_code="AZ", admin_levels=[8])

        legacy = CITY.replace(b"3600000042", str(area_id).encode()).replace(
            b'<relation id="42">', f'<relation id="{relation_id}">'.encode())
        legacy_result = self.provider(httpx.Response(200, content=legacy)).fetch_city_extract(
            relation_id, selector_profile=DEFAULT_PROFILE, country_code="AZ", admin_levels=[8])
        self.assertEqual(legacy_result["xml"], legacy)

    def test_disabled_mode_and_endpoint_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_city_geography_provider())
        with patch.dict(os.environ, {"CITY_GEOGRAPHY_PROVIDER": "overpass"}, clear=True):
            self.assertIsInstance(get_city_geography_provider(), CityGeographyProvider)
        for endpoint in ("http://example.com", "https://user:secret@example.com", "https://example.com/#fragment", "https://example.com/?query"):
            with self.subTest(endpoint=endpoint), self.assertRaises(CityGeographyError):
                CityGeographyProvider(overpass_url=endpoint)

    def test_profiles_are_captured_and_special_overrides_only_narrow_levels(self):
        self.assertEqual(country_admin_levels()["CA"], [6, 8])
        self.assertEqual(country_admin_levels()["GB"], [6, 8])
        self.assertEqual(country_admin_levels()["US"], [4, 5, 8])
        self.assertEqual(country_selector_profile("CA"), "ca-municipal-v1")
        self.assertEqual(country_selector_profile("GB"), "gb-local-authority-v1")
        self.assertEqual(country_selector_profile("US"), "us-municipal-v1")
        with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": '{"CA":[8],"US":[8],"GB":[6]}'}, clear=True):
            policies = country_admin_levels()
        self.assertEqual(policies["CA"], [8])
        self.assertEqual(policies["US"], [8])
        self.assertEqual(policies["GB"], [6])
        for override in ('{"CA":[4]}', '{"GB":[10]}', '{"US":[6]}'):
            with patch.dict(os.environ, {"CITY_ADMIN_LEVELS": override}, clear=True):
                with self.subTest(override=override), self.assertRaisesRegex(CityGeographyError, "invalid_city_country_policy"):
                    country_admin_levels()
        # Previously queued US tasks retain the legacy numeric-only selector.
        result = self.provider(httpx.Response(200, json=COUNTRY)).fetch_country_catalog("US", [8])
        self.assertEqual(result["cities"][0]["relation_id"], 42)

    def test_gb_catalog_uses_gss_authority_codes_and_checks_level_and_designation(self):
        payload = {
            "osm3s": COUNTRY["osm3s"],
            "elements": [COUNTRY["elements"][0],
                {"type": "relation", "id": 50,
                 "tags": {"name": "Bristol", "boundary": "administrative", "admin_level": "6",
                          "ref:gss": "E06000023", "designation": "unitary_authority"},
                 "bounds": COUNTRY["elements"][1]["bounds"]}],
        }
        requests = []
        result = self.provider(httpx.Response(200, json=payload), requests).fetch_country_catalog(
            "GB", [6, 8], selector_profile="gb-local-authority-v1")
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn('"ref:gss"~"^(E0[6-9]|W06|S12|N09)[0-9]{6}$"', query)
        self.assertNotIn("E10", query)
        self.assertEqual([row["relation_id"] for row in result["cities"]], [50])
        for reference, level, designation in (
            ("E10000001", "6", "non_metropolitan_county"),
            ("E05000001", "8", "ward"),
            ("E06000023", "8", "unitary_authority"),
        ):
            invalid = json.loads(json.dumps(payload))
            tags = invalid["elements"][1]["tags"]
            tags.update({"ref:gss": reference, "admin_level": level, "designation": designation})
            with self.subTest(reference=reference, level=level), self.assertRaises(CityGeographyError):
                self.provider(httpx.Response(200, json=invalid)).fetch_country_catalog(
                    "GB", [6, 8], selector_profile="gb-local-authority-v1")
        self.assertTrue(relation_matches("GB", "gb-local-authority-v1", [6, 8], {
            "boundary": "administrative", "admin_level": "6", "ref:gss": "E09000001", "designation": "sui_generis"}))
        self.assertTrue(relation_matches("GB", "gb-local-authority-v1", [6, 8], {
            "boundary": "administrative", "admin_level": "8", "ref:gss": "N09000001"}))
        self.assertFalse(relation_matches("GB", "gb-local-authority-v1", [6, 8], {
            "boundary": "administrative", "admin_level": "6", "ref:gss": "E06000023", "designation": ["ward"]}))

    def test_us_catalog_selects_level8_and_exact_exception_relations_only(self):
        payload = {
            "osm3s": COUNTRY["osm3s"],
            "elements": [COUNTRY["elements"][0],
                {"type": "relation", "id": 80, "tags": {"name": "Smallville", "boundary": "administrative",
                 "admin_level": "8", "place": "city"}, "bounds": COUNTRY["elements"][1]["bounds"]},
                {"type": "relation", "id": 175905, "tags": {"name": "New York", "boundary": "administrative",
                 "admin_level": "5", "wikidata": "Q60", "border_type": "city"}, "bounds": COUNTRY["elements"][1]["bounds"]},
                {"type": "relation", "id": 162069, "tags": {"name": "District of Columbia", "boundary": "administrative",
                 "admin_level": "4", "ISO3166-2": "US-DC"}, "bounds": COUNTRY["elements"][1]["bounds"]}],
        }
        requests = []
        result = self.provider(httpx.Response(200, json=payload), requests).fetch_country_catalog(
            "US", [4, 5, 8], selector_profile="us-municipal-v1")
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn("rel(175905)", query)
        self.assertIn("rel(162069)", query)
        self.assertIn('"ISO3166-1"="US"', query)
        self.assertEqual({row["relation_id"] for row in result["cities"]}, {80, 175905, 162069})

        narrow_requests = []
        narrow_payload = {"osm3s": COUNTRY["osm3s"], "elements": [COUNTRY["elements"][0], payload["elements"][2]]}
        self.provider(httpx.Response(200, json=narrow_payload), narrow_requests).fetch_country_catalog(
            "US", [5], selector_profile="us-municipal-v1")
        narrow_query = parse_qs(narrow_requests[0].content.decode())["data"][0]
        self.assertIn("rel(175905)", narrow_query)
        self.assertNotIn('admin_level"="8', narrow_query)
        self.assertNotIn("rel(162069)", narrow_query)

        nonmunicipal = json.loads(json.dumps(payload))
        nonmunicipal["elements"][1]["tags"]["place"] = "county"
        self.assertEqual({row["relation_id"] for row in self.provider(httpx.Response(200, json=nonmunicipal)).fetch_country_catalog(
            "US", [4, 5, 8], selector_profile="us-municipal-v1")["cities"]}, {175905, 162069})
        consolidated = json.loads(json.dumps(payload))
        consolidated["elements"][1]["tags"]["border_type"] = "city;county"
        self.assertIn(80, {row["relation_id"] for row in self.provider(httpx.Response(200, json=consolidated)).fetch_country_catalog(
            "US", [4, 5, 8], selector_profile="us-municipal-v1")["cities"]})
        for key in ("border_type", "designation"):
            nonmunicipal = json.loads(json.dumps(payload))
            nonmunicipal["elements"][1]["tags"][key] = "county"
            self.assertEqual({row["relation_id"] for row in self.provider(httpx.Response(200, json=nonmunicipal)).fetch_country_catalog(
                "US", [4, 5, 8], selector_profile="us-municipal-v1")["cities"]}, {175905, 162069})

    def test_canada_catalog_requires_all_province_scope_markers_and_proof(self):
        bounds = COUNTRY["elements"][1]["bounds"]
        relation = {"type": "relation", "id": 42,
            "tags": {"name": "Victoria", "boundary": "administrative", "admin_level": "8"},
            "bounds": bounds, "members": [{"type": "node", "ref": 9, "role": "admin_centre"}]}
        payload = canada_catalog({"CA-BC": [42]}, [relation])
        requests = []
        result = self.provider(httpx.Response(200, json=payload), requests).fetch_country_catalog(
            "CA", [6, 8], selector_profile="ca-municipal-v1")
        query = parse_qs(requests[0].content.decode())["data"][0]
        self.assertIn('"ISO3166-2"~"^CA-(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$"', query)
        self.assertIn('.province out tags;', query)
        self.assertIn('make scope;out;', query)
        self.assertNotIn('relation_ids=', query)
        self.assertIn("out body bb", query)
        self.assertTrue(all(set(element) == {"type", "id"} for element in payload["elements"]
                            if element["type"] == "scope"))
        self.assertEqual(result["cities"][0]["selection_scope"], "CA-BC")

        for mutate in (
            lambda value: value["elements"].pop(1),
            lambda value: value["elements"][1]["tags"].update({"ISO3166-2": "CA-BC"}),
            lambda value: next(element for element in value["elements"] if element.get("id") == 42)["members"].clear(),
            lambda value: value["elements"][-1].update(tags={"relation_ids": "999"}),
        ):
            invalid = json.loads(json.dumps(payload)); mutate(invalid)
            with self.subTest(mutate=mutate), self.assertRaises(CityGeographyError):
                self.provider(httpx.Response(200, json=invalid)).fetch_country_catalog(
                    "CA", [6, 8], selector_profile="ca-municipal-v1")

    def test_canada_filters_unproven_types_deduplicates_identical_border_relations_and_rejects_conflicts(self):
        bounds = COUNTRY["elements"][1]["bounds"]
        relation = {"type": "relation", "id": 62,
                    "tags": {"name": "Border Municipality", "boundary": "administrative", "admin_level": "6", "place": "municipality"},
                    "bounds": bounds, "members": []}
        payload = canada_catalog({"CA-AB": [62], "CA-SK": [62]},
                                 [json.loads(json.dumps(relation)), json.loads(json.dumps(relation))])
        result = self.provider(httpx.Response(200, json=payload)).fetch_country_catalog("CA", [6], selector_profile="ca-municipal-v1")
        self.assertEqual(len(result["cities"]), 1)
        self.assertEqual(result["cities"][0]["selection_scope"], "CA-AB")
        next(element for element in payload["elements"] if element.get("type") == "relation" and element.get("id") == 62)["bounds"]["maxlon"] = 5.
        with self.assertRaisesRegex(CityGeographyError, "invalid_city_provider_response"):
            self.provider(httpx.Response(200, json=payload)).fetch_country_catalog("CA", [6], selector_profile="ca-municipal-v1")

        border_relations = [element for element in payload["elements"] if element.get("type") == "relation" and element.get("id") == 62]
        border_relations[0]["bounds"]["maxlon"] = 4.5
        border_relations[0]["tags"]["place"] = "county"
        border_relations[1]["tags"]["place"] = "county"
        with self.assertRaisesRegex(CityGeographyError, "city_country_boundary_unavailable"):
            self.provider(httpx.Response(200, json=payload)).fetch_country_catalog("CA", [6], selector_profile="ca-municipal-v1")

    def test_canada_province_rules_accept_positive_municipal_types_and_reject_wrong_tiers(self):
        bounds = COUNTRY["elements"][1]["bounds"]
        specs = [
            (101, "CA-BC", "8", None, [{"type": "node", "ref": 1, "role": "admin_centre"}]),
            (102, "CA-ON", "8", "city", []),
            (103, "CA-QC", "8", "town", []),
            (104, "CA-QC", "6", "city", []),
            (105, "CA-AB", "8", "village", []),
            (106, "CA-NU", "6", "city", []),
            (107, "CA-NU", "8", "hamlet", []),
            (108, "CA-MB", "6", "municipality", []),
            (109, "CA-SK", "6", "county", []),
        ]
        relations = []
        scopes = {}
        for identifier, scope, level, place, members in specs:
            tags = {"name": f"Town {identifier}", "boundary": "administrative", "admin_level": level}
            if place is not None:
                tags["place"] = place
            relations.append({"type": "relation", "id": identifier, "tags": tags, "bounds": bounds, "members": members})
            scopes.setdefault(scope, []).append(identifier)
        result = self.provider(httpx.Response(200, json=canada_catalog(scopes, relations))).fetch_country_catalog(
            "CA", [6, 8], selector_profile="ca-municipal-v1")
        self.assertEqual({row["relation_id"] for row in result["cities"]}, {101, 102, 103, 106, 107, 108})

    def test_final_city_extract_revalidates_captured_profile_and_canadian_admin_centre(self):
        gb = CITY.replace(b'<tag k="admin_level" v="8"/>', b'<tag k="admin_level" v="6"/><tag k="ref:gss" v="E06000023"/><tag k="designation" v="unitary_authority"/>')
        self.provider(httpx.Response(200, content=gb)).fetch_city_extract(
            42, selector_profile="gb-local-authority-v1", country_code="GB", admin_levels=[6, 8])
        us = CITY.replace(b'<relation id="42">', b'<relation id="175905">')
        us = us.replace(b"3600000042", b"3600175905")
        us = us.replace(b'<tag k="admin_level" v="8"/>', b'<tag k="admin_level" v="5"/><tag k="wikidata" v="Q60"/><tag k="border_type" v="city"/>')
        self.provider(httpx.Response(200, content=us)).fetch_city_extract(
            175905, selector_profile="us-municipal-v1", country_code="US", admin_levels=[4, 5, 8])
        canada = CITY.replace(b'<tag k="admin_level" v="8"/>', b'<tag k="admin_level" v="8"/><tag k="place" v="municipality"/>')
        canada = canada.replace(b'<relation id="42">', b'<relation id="42"><member type="node" ref="9" role="admin_centre"/>')
        self.provider(httpx.Response(200, content=canada)).fetch_city_extract(
            42, selector_profile="ca-municipal-v1", country_code="CA", admin_levels=[6, 8], selection_scope="CA-BC")
        dc = CITY.replace(b'<relation id="42">', b'<relation id="162069">')
        dc = dc.replace(b"3600000042", b"3600162069")
        dc = dc.replace(b'<tag k="admin_level" v="8"/>', b'<tag k="admin_level" v="4"/><tag k="ISO3166-2" v="US-DC"/>')
        self.provider(httpx.Response(200, content=dc)).fetch_city_extract(
            162069, selector_profile="us-municipal-v1", country_code="US", admin_levels=[4, 5, 8])
        for bad_payload, kwargs in (
            (gb.replace(b'E06000023', b'E10000001'), {"selector_profile": "gb-local-authority-v1", "country_code": "GB", "admin_levels": [6, 8]}),
            (canada.replace(b'admin_centre', b'outer'), {"selector_profile": "ca-municipal-v1", "country_code": "CA", "admin_levels": [6, 8], "selection_scope": "CA-BC"}),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CityGeographyError, "city_boundary_unavailable"):
                self.provider(httpx.Response(200, content=bad_payload)).fetch_city_extract(42, **kwargs)


if __name__ == "__main__":
    unittest.main()
