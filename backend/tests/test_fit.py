import struct
import unittest

import fitdecode

from backend.app.fit import _timestamp, parse_fit
from backend.app.gpx import GpxError


def fit_fixture(*, file_type=4, records=(), events=(), sport=1, endian="<",
                compressed=False, unknown_count=0, developer_collision=False):
    def definition(local, global_num, fields):
        architecture = 0 if endian == "<" else 1
        return (bytes((0x40 | local, 0, architecture)) + struct.pack(endian + "H", global_num)
                + bytes((len(fields),)) + b"".join(bytes(field) for field in fields))

    def message(local, *values):
        return bytes((local,)) + b"".join(values)

    def u32(value):
        return struct.pack(endian + "I", value)

    def i32(value):
        return struct.pack(endian + "i", value)

    body = bytearray()
    body.extend(definition(0, 0, ((0, 1, 0), (1, 2, 0x84), (2, 2, 0x84), (4, 4, 0x86))))
    body.extend(message(0, bytes((file_type,)), struct.pack(endian + "H", 1), struct.pack(endian + "H", 1), u32(1_167_609_600)))
    if sport is not None:
        body.extend(definition(2, 18, ((5, 1, 0),)))
        body.extend(message(2, bytes((sport,))))
    body.extend(definition(1, 21, ((253, 4, 0x86), (0, 1, 0), (1, 1, 0))))
    if developer_collision:
        body.extend(definition(4, 207, ((3, 1, 0),)))
        body.extend(message(4, b"\x00"))
        body.extend(definition(5, 206, ((0, 1, 0), (1, 1, 0), (2, 1, 0), (3, 16, 0x07))))
        body.extend(message(5, b"\x00", b"\x00", b"\x85", b"position_lat\x00\x00\x00\x00"))
        architecture = 0 if endian == "<" else 1
        body.extend(bytes((0x60 | 3, 0, architecture)) + struct.pack(endian + "H", 20))
        body.extend(bytes((2, 253, 4, 0x86, 1, 4, 0x85, 1, 0, 4, 0)))
    else:
        body.extend(definition(3, 20, ((253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85))))
    records = list(records)
    events_by_index = {}
    for index, timestamp, event_type in events:
        events_by_index.setdefault(index, []).append((timestamp, event_type))
    for index, (timestamp, latitude, longitude) in enumerate(records):
        for event_timestamp, event_type in events_by_index.get(index, ()):
            body.extend(message(1, u32(event_timestamp), b"\x00", bytes((event_type,))))
        if developer_collision:
            body.extend(message(3, u32(timestamp), i32(longitude), i32(latitude)))
        elif compressed and index > 0:
            if index == 1:
                body.extend(definition(3, 20, ((0, 4, 0x85), (1, 4, 0x85))))
            body.extend(bytes((0x80 | (3 << 5) | (timestamp & 0x1F),))
                        + i32(latitude) + i32(longitude))
        else:
            body.extend(message(3, u32(timestamp), i32(latitude), i32(longitude)))
    if unknown_count:
        body.extend(definition(4, 9999, ()))
        body.extend(bytes((4,)) * unknown_count)
    for event_timestamp, event_type in events_by_index.get(len(records), ()):
        body.extend(message(1, u32(event_timestamp), b"\x00", bytes((event_type,))))

    header = bytes((14, 0x20)) + struct.pack("<HI", 21_171, len(body)) + b".FIT"
    header += struct.pack("<H", fitdecode.utils.compute_crc(header))
    stream = header + body
    return stream + struct.pack("<H", fitdecode.utils.compute_crc(stream))


def semicircles(degrees):
    return round(degrees * (1 << 31) / 180)


class FitParserTests(unittest.TestCase):
    def test_names_fit_activities_from_available_sport_and_date(self):
        record = ((1_167_609_600, semicircles(50), semicircles(4)),)
        self.assertEqual(parse_fit(fit_fixture(records=record)).name, "Run · 2026-12-31")
        self.assertEqual(parse_fit(fit_fixture(records=record, sport=None)).name, "FIT activity · 2026-12-31")
        self.assertEqual(parse_fit(fit_fixture(records=((1, semicircles(50), semicircles(4)),))).name, "Run (FIT)")

    def test_converts_coordinates_preserves_timestamps_and_timer_gaps(self):
        t0 = 1_167_609_600
        data = fit_fixture(
            records=((t0 + i, semicircles(50 + i / 10), semicircles(4 + i / 10))
                     for i in range(4)),
            events=((0, t0, 0), (2, t0 + 2, 1), (3, t0 + 3, 0)),
        )
        parsed = parse_fit(data)
        self.assertEqual(len(parsed.tracks), 2)
        self.assertEqual([len(segment) for segment in parsed.tracks], [2, 1])
        self.assertAlmostEqual(parsed.tracks[0][0][0], 4.0, places=6)
        self.assertAlmostEqual(parsed.tracks[0][0][1], 50.0, places=6)
        self.assertEqual(parsed.timestamps[0][0], "2026-12-31T00:00:00Z")
        self.assertEqual(parsed.activity_type, "running")
        self.assertEqual(parsed.date, "2026-12-31")

    def test_endian_and_compressed_timestamp_rollover(self):
        first = 0x1000001E
        data = fit_fixture(endian=">", compressed=True, records=(
            (first, semicircles(50), semicircles(4)),
            (0x10000021, semicircles(50.1), semicircles(4.1)),
        ))
        parsed = parse_fit(data)
        self.assertEqual(len(parsed.tracks[0]), 2)
        self.assertEqual(parsed.timestamps[0][1], "1998-07-03T21:24:49Z")

    def test_rejects_relative_timestamp_and_developer_field_name_collision(self):
        self.assertIsNone(_timestamp(1))
        relative_time = parse_fit(fit_fixture(records=((1, semicircles(50), semicircles(4)),)))
        self.assertEqual(relative_time.timestamps, [[None]])
        with self.assertRaisesRegex(GpxError, "fit_no_track_points"):
            parse_fit(fit_fixture(developer_collision=True, records=(
                (1_167_609_600, semicircles(50), semicircles(4)),
            )))

    def test_bounds_total_fit_messages_including_unknown_messages(self):
        data = fit_fixture(records=((1_167_609_600, semicircles(50), semicircles(4)),),
                           unknown_count=10)
        with self.assertRaisesRegex(GpxError, "fit_message_limit"):
            parse_fit(data, max_messages=8)

    def test_missing_position_breaks_segment_and_no_gps_is_safe_failure(self):
        t0 = 1_167_609_600
        parsed = parse_fit(fit_fixture(records=(
            (t0, semicircles(50), semicircles(4)),
            (t0 + 1, 0x7FFFFFFF, semicircles(4)),
            (t0 + 2, semicircles(51), semicircles(5)),
        )))
        self.assertEqual(len(parsed.tracks), 2)
        with self.assertRaises(GpxError) as error:
            parse_fit(fit_fixture(records=()))
        self.assertEqual(error.exception.code, "fit_no_track_points")

    def test_rejects_non_activity_bad_crc_truncation_chained_and_limits(self):
        t0 = 1_167_609_600
        valid = fit_fixture(records=((t0, semicircles(50), semicircles(4)),))
        with self.assertRaisesRegex(GpxError, "fit_not_activity"):
            parse_fit(fit_fixture(file_type=6))
        with self.assertRaisesRegex(GpxError, "invalid_fit_file"):
            parse_fit(valid[:-1])
        corrupt = valid[:-1] + bytes((valid[-1] ^ 1,))
        with self.assertRaises(GpxError):
            parse_fit(corrupt)
        with self.assertRaisesRegex(GpxError, "fit_multiple_streams"):
            parse_fit(valid + valid)
        with self.assertRaisesRegex(GpxError, "fit_record_limit"):
            parse_fit(valid, max_records=0)


if __name__ == "__main__":
    unittest.main()
