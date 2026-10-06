import unittest
import urllib.error
import os
import xml.etree.ElementTree as ET
from unittest.mock import patch
from fastapi import HTTPException

from backend.app.routes import (MAX_PROVIDER_BYTES, MAX_ROUTE_POSITIONS, RouteCreateRequest,
                                RouteGeometry, RoutePreviewRequest, _calculate_route, _gpx,
                                _provider_request)


def osrm_response(waypoints):
    return {
        "code": "Ok",
        "waypoints": [{"location": point, "distance": 0.0} for point in waypoints],
        "routes": [{
            "distance": 125.5,
            "duration": 94.0,
            "geometry": {"type": "LineString", "coordinates": waypoints},
        }],
    }


class RouteValidationTests(unittest.TestCase):
    def test_preview_response_is_validated_and_echoes_revision(self):
        waypoints = [[4.0, 50.0], [4.01, 50.01]]
        result = _calculate_route(None, waypoints, 7, osrm_response)
        self.assertEqual(result.client_revision, 7)
        self.assertEqual(result.provider, "test")
        self.assertEqual(result.geometry.type, "LineString")
        self.assertEqual(result.distance_m, 125.5)

    def test_strict_revision_and_coordinate_validation(self):
        with self.assertRaises(ValueError):
            RoutePreviewRequest.model_validate({"waypoints": [[4, 50], [4, 50.1]], "client_revision": True})
        for points in (
            [[True, 50], [4, 50.1]],
            [[181, 50], [4, 50.1]],
            [[4, float("nan")], [4, 50.1]],
            [[4, 50]],
            [[10**1000, 50], [4, 50.1]],
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                RoutePreviewRequest.model_validate({"waypoints": points, "client_revision": 0})
        with self.assertRaises(ValueError):
            RouteCreateRequest.model_validate({"name": "bad\x01name", "waypoints": [[4, 50], [4, 50.1]]})

    def test_no_route_and_waypoint_outside_snap_radius(self):
        with self.assertRaises(HTTPException) as no_route:
            _calculate_route(None, [[4.0, 50.0], [4.1, 50.0]], 0, lambda _: {"code": "NoRoute"})
        self.assertEqual(no_route.exception.status_code, 422)
        far = osrm_response([[4.02, 50.0], [4.1, 50.0]])
        with self.assertRaises(HTTPException) as unmatched:
            _calculate_route(None, [[4.0, 50.0], [4.1, 50.0]], 0, lambda _: far)
        self.assertEqual(unmatched.exception.status_code, 422)

    def test_invalid_provider_geometry_metrics_and_snaps_fail_closed(self):
        valid = osrm_response([[4.0, 50.0], [4.1, 50.0]])
        cases = []
        bad_geometry = {**valid, "routes": [{**valid["routes"][0], "geometry": {"type": "MultiLineString", "coordinates": []}}]}
        cases.append(bad_geometry)
        bad_distance = {**valid, "routes": [{**valid["routes"][0], "distance": float("inf")}]}
        cases.append(bad_distance)
        huge_metric = {**valid, "routes": [{**valid["routes"][0], "duration": 10**1000}]}
        cases.append(huge_metric)
        bad_count = {**valid, "waypoints": valid["waypoints"][:1]}
        cases.append(bad_count)
        malformed_code = {"code": [], "waypoints": [], "routes": []}
        cases.append(malformed_code)
        for result in cases:
            with self.subTest(result=result), self.assertRaises(HTTPException) as error:
                _calculate_route(None, [[4.0, 50.0], [4.1, 50.0]], 0, lambda _points, r=result: r)
            self.assertEqual(error.exception.status_code, 503)

    def test_http_no_route_is_classified_and_url_sets_snap_limit(self):
        error = urllib.error.HTTPError("https://routing.invalid", 400, "Bad Request", {}, None)
        error.read = lambda _size: b'{"code":"NoSegment"}'
        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes.urllib.request.urlopen", side_effect=error) as open_url:
            with self.assertRaises(HTTPException) as result:
                _provider_request([[4.0, 50.0], [4.01, 50.01]])
        self.assertEqual(result.exception.status_code, 422)
        url = open_url.call_args.args[0].full_url
        self.assertIn("radiuses=100;100", url)

    def test_http_error_with_unhashable_code_is_safe_provider_failure(self):
        error = urllib.error.HTTPError("https://routing.invalid", 400, "Bad Request", {}, None)
        error.read = lambda _size: b'{"code":[]}'
        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HTTPException) as result:
                _provider_request([[4.0, 50.0], [4.01, 50.01]])
        self.assertEqual(result.exception.status_code, 503)

    def test_degenerate_line_is_a_safe_no_route(self):
        degenerate = osrm_response([[4.0, 50.0], [4.0, 50.0]])
        with self.assertRaises(HTTPException) as result:
            _calculate_route(None, [[4.0, 50.0], [4.0, 50.0]], 0, lambda _: degenerate)
        self.assertEqual(result.exception.status_code, 422)

    def test_provider_timeout_byte_and_geometry_bounds(self):
        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes.urllib.request.urlopen", side_effect=TimeoutError("slow")) as open_url:
            with self.assertRaises(HTTPException) as result:
                _provider_request([[4.0, 50.0], [4.01, 50.01]])
        self.assertEqual(result.exception.status_code, 504)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 8)

        class LargeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, size):
                self.requested_size = size
                return b" " * size

        large_response = LargeResponse()
        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes.urllib.request.urlopen", return_value=large_response):
            with self.assertRaises(HTTPException) as large:
                _provider_request([[4.0, 50.0], [4.01, 50.01]])
        self.assertEqual((large.exception.status_code, large_response.requested_size),
                         (503, MAX_PROVIDER_BYTES + 1))

        class DeepResponse(LargeResponse):
            def read(self, _size):
                return b'{"code":' + b"[" * 10000 + b"]" * 10000 + b"}"

        with patch.dict(os.environ, {"ROUTING_PROVIDER": "fossgis"}), \
                patch("backend.app.routes.urllib.request.urlopen", return_value=DeepResponse()):
            with self.assertRaises(HTTPException) as deep:
                _provider_request([[4.0, 50.0], [4.01, 50.01]])
        self.assertEqual(deep.exception.status_code, 503)

        oversized = osrm_response([[4.0, 50.0], [4.01, 50.01]])
        oversized["routes"][0]["geometry"]["coordinates"] = [[4.0, 50.0]] * (MAX_ROUTE_POSITIONS + 1)
        with self.assertRaises(HTTPException) as geometry:
            _calculate_route(None, [[4.0, 50.0], [4.01, 50.01]], 0, lambda _: oversized)
        self.assertEqual(geometry.exception.status_code, 503)

    def test_gpx_decimal_coordinates_do_not_use_exponents_and_wrap_180(self):
        geometry = RouteGeometry.model_validate({"type": "LineString", "coordinates": [
            [0.00000001, 0.0], [180.0, 1.0],
        ]})
        root = ET.fromstring(_gpx("Route", geometry))
        namespace = {"g": "http://www.topografix.com/GPX/1/1"}
        points = root.findall(".//g:rtept", namespace)
        self.assertEqual(points[0].attrib, {"lat": "0.0", "lon": "0.00000001"})
        self.assertEqual(points[1].attrib["lon"], "-180.0")


if __name__ == "__main__":
    unittest.main()
