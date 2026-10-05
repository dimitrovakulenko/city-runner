import asyncio
import unittest
import stat
import tempfile
from pathlib import Path

from backend.app.gpx import GpxError, parse_gpx
from backend.app.storage import LocalObjectStore
from backend.app.uploads import MAX_UPLOAD_REQUEST_BYTES, _one_gpx_file
from fastapi import HTTPException
from starlette.requests import Request


class GpxParserTests(unittest.TestCase):
    def request(self, content, *, chunk_size=8192):
        boundary = b"gpx-test-boundary"
        body = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"run.gpx\"\r\n"
                b"Content-Type: application/gpx+xml\r\n\r\n" + content + b"\r\n--" + boundary + b"--\r\n")
        # The Request stream consumes each supplied message once. Build the
        # generator here so no Content-Length header hides the streaming limit.
        async def stream_receive():
            for index in range(0, len(body), chunk_size):
                yield {"type": "http.request", "body": body[index:index + chunk_size],
                       "more_body": index + chunk_size < len(body)}
            yield {"type": "http.disconnect"}

        iterator = stream_receive().__aiter__()

        async def actual_receive():
            return await iterator.__anext__()

        return Request({"type": "http", "method": "POST", "path": "/api/uploads",
                        "headers": [(b"content-type", b"multipart/form-data; boundary=" + boundary)]}, actual_receive)

    def test_chunked_multipart_body_is_bounded_before_parse_finishes(self):
        content = b"<gpx><trk><trkseg><trkpt lat='0' lon='0'/></trkseg></trk></gpx>"
        self.assertEqual(asyncio.run(_one_gpx_file(self.request(content))), content)
        with self.assertRaises(HTTPException) as error:
            asyncio.run(_one_gpx_file(self.request(b"x" * (MAX_UPLOAD_REQUEST_BYTES + 1))))
        self.assertEqual(error.exception.status_code, 413)

    def test_namespaced_tracks_preserve_segments_gaps_and_nullable_times(self):
        content = b'''<?xml version="1.0"?>
        <gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
          <metadata><name>Morning run</name></metadata>
          <trk><type>Running</type><trkseg>
            <trkpt lat="50.1" lon="4.2"><time>2026-10-04T08:00:00Z</time></trkpt>
            <trkpt lat="50.2" lon="4.3" />
          </trkseg><trkseg>
            <trkpt lat="51" lon="5"><time>2026-10-04T08:05:00+00:00</time></trkpt>
          </trkseg></trk>
        </gpx>'''
        result = parse_gpx(content)
        self.assertEqual(result.name, "Morning run")
        self.assertEqual(result.activity_type, "running")
        self.assertEqual(result.date, "2026-10-04")
        self.assertEqual(result.point_count, 3)
        self.assertEqual(result.tracks, [[[4.2, 50.1], [4.3, 50.2]], [[5.0, 51.0]]])
        self.assertEqual(result.timestamps, [
            ["2026-10-04T08:00:00Z", None], ["2026-10-04T08:05:00+00:00"],
        ])

    def test_local_storage_uses_private_modes_and_rejects_client_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            store = LocalObjectStore(root)
            key = store.write(b"<gpx/>")
            self.assertEqual(store.read(key), b"<gpx/>")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / key).stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                store.read("../outside.gpx")

    def test_missing_metadata_is_unknown_and_planned_route_is_not_an_activity(self):
        result = parse_gpx(b'''<gpx><trk><trkseg><trkpt lat="1" lon="2"/>
            <trkpt lat="3" lon="4"/></trkseg></trk></gpx>''')
        self.assertEqual(result.name, "Unknown activity")
        self.assertEqual(result.activity_type, "unknown")
        self.assertEqual(result.date, "unknown")
        self.assertEqual(result.tracks, [[[2.0, 1.0], [4.0, 3.0]]])
        self.assertEqual(result.timestamps, [[None, None]])
        with self.assertRaises(GpxError):
            parse_gpx(b'''<gpx><rte><rtept lat="1" lon="2"/></rte></gpx>''')

    def test_malformed_or_unsafe_xml_is_rejected(self):
        for content in (
            b"<gpx><trk>",
            b"<!DOCTYPE gpx><gpx/>",
            b'<!DOCTYPE gpx [<!ENTITY secret SYSTEM "file:///etc/passwd">]><gpx>&secret;</gpx>',
            b"<not-gpx/>",
            b"<gpx><wpt lat='0' lon='0'/></gpx>",
        ):
            with self.subTest(content=content[:20]), self.assertRaises(GpxError):
                parse_gpx(content)

    def test_coordinate_and_timestamp_validation(self):
        for point in (
            b"<trkpt lat='91' lon='0'/>",
            b"<trkpt lat='0' lon='181'/>",
            b"<trkpt lat='nan' lon='0'/>",
        ):
            with self.subTest(point=point), self.assertRaises(GpxError):
                parse_gpx(b"<gpx><trk><trkseg>" + point + b"</trkseg></trk></gpx>")

        for invalid_time in (
            "2026-10-04", "2026-10-04T08:00:00", "2026-10-04T08:00:00Zjunk",
            "2026-10-04T08:00:00+00:99", "yesterday",
        ):
            result = parse_gpx(("<gpx><trk><trkseg><trkpt lat='0' lon='0'><time>" +
                                invalid_time + "</time></trkpt></trkseg></trk></gpx>").encode())
            self.assertEqual(result.timestamps, [[None]])
            self.assertEqual(result.date, "unknown")

    def test_byte_and_point_limits_are_enforced(self):
        with self.assertRaises(GpxError) as byte_error:
            parse_gpx(b"<gpx/>", max_bytes=5)
        self.assertEqual(byte_error.exception.code, "invalid_gpx_size")
        content = b"<gpx><trk><trkseg><trkpt lat='0' lon='0'/><trkpt lat='0' lon='1'/></trkseg></trk></gpx>"
        with self.assertRaises(GpxError) as point_error:
            parse_gpx(content, max_points=1)
        self.assertEqual(point_error.exception.code, "gpx_point_limit")


if __name__ == "__main__":
    unittest.main()
