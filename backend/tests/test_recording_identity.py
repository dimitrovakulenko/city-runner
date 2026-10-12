from __future__ import annotations

import unittest

from backend.app.recording_identity import canonical_recording, same_recording


class RecordingIdentityTests(unittest.TestCase):
    def fixture(self):
        return (
            [[[0.0, 51.0], [4.0, 52.0]], [[4.0, 52.0], [5.0, 53.0]]],
            [["2026-04-03T12:00:00Z", "2026-04-03T12:00:01.120000Z"],
             ["2026-04-03T12:00:01.130000Z", "2026-04-03T12:00:02Z"]],
            "running",
        )

    def test_offset_and_zero_fraction_variants_have_same_identity(self):
        tracks, timestamps, activity_type = self.fixture()
        first = canonical_recording(tracks, timestamps, activity_type)
        alternate = [["2026-04-03T14:00:00.000000+02:00",
                      "2026-04-03T14:00:01.12+02:00"],
                     ["2026-04-03T14:00:01.130000000+02:00",
                      "2026-04-03T14:00:02.000+02:00"]]
        second = canonical_recording(tracks, alternate, activity_type)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(same_recording(first, second))

    def test_signed_zero_is_normalized_but_coordinates_remain_exact(self):
        tracks, timestamps, activity_type = self.fixture()
        negative_zero_tracks = [[[-0.0, 51.0], [4.0, 52.0]], [[4.0, 52.0], [5.0, 53.0]]]
        first = canonical_recording(tracks, timestamps, activity_type)
        second = canonical_recording(negative_zero_tracks, timestamps, activity_type)
        changed = [[[-1e-15, 51.0], [4.0, 52.0]], [[4.0, 52.0], [5.0, 53.0]]]
        other = canonical_recording(changed, timestamps, activity_type)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.fingerprint, other.fingerprint)

    def test_segment_shape_time_order_type_and_point_count_are_identity_requirements(self):
        tracks, timestamps, activity_type = self.fixture()
        original = canonical_recording(tracks, timestamps, activity_type)
        split = canonical_recording([tracks[0] + tracks[1]], [timestamps[0] + timestamps[1]], activity_type)
        other_type = canonical_recording(tracks, timestamps, "walking")
        descending = canonical_recording(tracks, [[timestamps[0][1], timestamps[0][0]], timestamps[1]], activity_type)
        singleton = canonical_recording([[[0.0, 51.0]]], [[timestamps[0][0]]], activity_type)
        self.assertNotEqual(original.fingerprint, split.fingerprint)
        self.assertNotEqual(original.fingerprint, other_type.fingerprint)
        self.assertIsNone(descending)
        self.assertIsNone(singleton)

    def test_incomplete_or_excess_precision_times_are_ineligible(self):
        tracks, timestamps, activity_type = self.fixture()
        missing = [[timestamps[0][0], None], timestamps[1]]
        high_precision = [["2026-04-03T12:00:00.0000001Z", timestamps[0][1]], timestamps[1]]
        invalid = [["not-a-time", timestamps[0][1]], timestamps[1]]
        self.assertIsNone(canonical_recording(tracks, missing, activity_type))
        self.assertIsNone(canonical_recording(tracks, high_precision, activity_type))
        self.assertIsNone(canonical_recording(tracks, invalid, activity_type))

    def test_malformed_coordinates_and_timestamp_order_are_ineligible(self):
        tracks, timestamps, activity_type = self.fixture()
        invalid_coordinates = [[[10**1000, 51.0], [4.0, 52.0]], tracks[1]]
        backward_boundary = [[timestamps[0][0], timestamps[0][1]],
                             ["2026-04-03T12:00:00Z", timestamps[1][1]]]
        self.assertIsNone(canonical_recording(invalid_coordinates, timestamps, activity_type))
        self.assertIsNone(canonical_recording(tracks, backward_boundary, activity_type))

    def test_utc_conversion_overflow_and_segment_bound_are_ineligible(self):
        tracks, timestamps, activity_type = self.fixture()
        underflow = [["0001-01-01T00:00:00+23:00", timestamps[0][1]], timestamps[1]]
        overflow = [["9999-12-31T23:59:59-23:00", timestamps[0][1]], timestamps[1]]
        self.assertIsNone(canonical_recording(tracks, underflow, activity_type))
        self.assertIsNone(canonical_recording(tracks, overflow, activity_type))
        too_many_segments = [[]] * 100_001
        self.assertIsNone(canonical_recording(too_many_segments, too_many_segments, activity_type))
        too_many_points = [[0.0, 0.0]] * 100_001
        self.assertIsNone(canonical_recording([too_many_points], [[None] * len(too_many_points)], activity_type))


if __name__ == "__main__":
    unittest.main()
