import math
import unittest

from backend.app.activity_statistics import (
    ACTIVITY_STATISTICS_VERSION,
    EARTH_RADIUS_M,
    backfill_activity_statistics,
    calculate_activity_statistics,
)


class ActivityStatisticsTests(unittest.TestCase):
    def test_backfill_bounds_reject_unsafe_arguments_before_database_access(self):
        for kwargs in (
            {"batch_size": 0}, {"batch_size": 21}, {"max_activities": 0},
            {"max_activities": 101}, {"after_id": True}, {"after_id": 9_223_372_036_854_775_808},
            {"after_id": -9_223_372_036_854_775_809},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                backfill_activity_statistics(None, "account", **kwargs)

    def test_known_haversine_distance_utc_duration_and_pace(self):
        result = calculate_activity_statistics(
            [[[0, 0], [1, 0]]],
            [["2026-01-01T01:00:00+01:00", "2026-01-01T00:01:00Z"]],
        )
        self.assertAlmostEqual(result.distance_m, EARTH_RADIUS_M * math.pi / 180, places=6)
        self.assertEqual(result.duration_s, 60)
        self.assertAlmostEqual(result.pace_s_per_km, 60_000 / result.distance_m)
        self.assertEqual(result.version, ACTIVITY_STATISTICS_VERSION)

    def test_segments_pause_gaps_and_stationary_distance(self):
        separated = calculate_activity_statistics(
            [[[0, 0], [0.001, 0]], [[10, 0], [10.001, 0]]],
            [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"],
             ["2026-01-01T01:00:00Z", "2026-01-01T01:00:20Z"]],
        )
        self.assertAlmostEqual(separated.distance_m, 2 * EARTH_RADIUS_M * math.radians(0.001), places=5)
        self.assertEqual(separated.duration_s, 30)
        stationary = calculate_activity_statistics(
            [[[4.2, 50.1], [4.2, 50.1]]],
            [["2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z"]],
        )
        self.assertEqual(stationary.distance_m, 0)
        self.assertEqual(stationary.duration_s, 30)
        self.assertIsNone(stationary.pace_s_per_km)

    def test_antimeridian_poles_and_singleton_no_track(self):
        across_date_line = calculate_activity_statistics(
            [[[179.9, 0], [-179.9, 0]]], [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"]]
        )
        self.assertAlmostEqual(across_date_line.distance_m, EARTH_RADIUS_M * math.radians(0.2), places=5)
        at_pole = calculate_activity_statistics(
            [[[-100, 90], [100, 90]]], [["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"]]
        )
        self.assertLess(at_pole.distance_m, 1e-6)
        tiny = calculate_activity_statistics(
            [[[0, 0], [1e-14, 0]]], [["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"]]
        )
        self.assertGreater(tiny.distance_m, 0)
        for tracks, timestamps in (([], []), ([[[1, 2]]], [["2026-01-01T00:00:00Z"]])):
            result = calculate_activity_statistics(tracks, timestamps)
            self.assertEqual((result.distance_m, result.duration_s, result.pace_s_per_km), (None, None, None))

    def test_bad_coordinates_or_track_shape_make_every_metric_unknown(self):
        for tracks in (
            None, {}, [["bad"]], [[[True, 0], [1, 0]]], [[[0, math.nan], [1, 0]]],
            [[[10**1000, 0], [1, 0]]], [[[181, 0], [1, 0]]], [[[[1, 2]]]],
        ):
            with self.subTest(tracks=repr(tracks)[:40]):
                result = calculate_activity_statistics(tracks, [["2026-01-01T00:00:00Z"]])
                self.assertEqual((result.distance_m, result.duration_s, result.pace_s_per_km), (None, None, None))

    def test_bounds_and_invalid_timing_only_remove_duration_and_pace(self):
        too_many_points = [[[0, 0]] * 100_001]
        too_many_segments = [[]] * 100_001
        self.assertIsNone(calculate_activity_statistics(too_many_points, []).distance_m)
        self.assertIsNone(calculate_activity_statistics(too_many_segments, []).distance_m)
        self.assertIsNone(calculate_activity_statistics([[[0, 0], [1, 0]]], []).duration_s)

        invalid_times = (
            ["2026-01-01T00:00:00Z", None],
            ["2026-01-01T00:00:00Z"],
            ["2026-01-01T00:00:01Z", "2026-01-01T00:00:00Z"],
            ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
            ["0001-01-01T00:00:00+23:00", "0001-01-01T00:00:01+23:00"],
            ["9999-12-31T23:59:58-23:00", "9999-12-31T23:59:59-23:00"],
        )
        for timestamps in invalid_times:
            with self.subTest(timestamps=timestamps):
                result = calculate_activity_statistics([[[0, 0], [1, 0]]], [timestamps])
                self.assertGreater(result.distance_m, 0)
                self.assertIsNone(result.duration_s)
                self.assertIsNone(result.pace_s_per_km)

    def test_parser_segment_and_missing_position_boundaries_are_preserved(self):
        first = [0, 50]
        middle = [0, 50.001]
        after_pause = [1, 51]
        final = [1, 51.001]
        stats = calculate_activity_statistics(
            [[first, middle], [after_pause, final]],
            [["2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z"],
             ["2026-01-01T01:00:00Z", "2026-01-01T01:00:10Z"]],
        )
        first_edge = EARTH_RADIUS_M * math.radians(0.001)
        self.assertAlmostEqual(stats.distance_m, 2 * first_edge, delta=2)
        self.assertEqual(stats.duration_s, 20)


if __name__ == "__main__":
    unittest.main()
