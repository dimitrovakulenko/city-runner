import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.geography import (
    Relation, Way, _parse_osm_stream, _read_osm_snapshot, _stitch_rings,
    eligible_way, parse_osm_xml,
)


class OsmParserTests(unittest.TestCase):
    def test_poc_eligibility_exclusions_and_no_private_foot_yes_exception(self):
        self.assertTrue(eligible_way({"name": "A", "highway": "residential"}))
        self.assertTrue(eligible_way({"name": "A", "highway": "footway", "access": "destination"}))
        for tags in (
            {"highway": "residential"},
            {"name": "A", "highway": "motorway"},
            {"name": "A", "highway": "motorway_link"},
            {"name": "A", "highway": "trunk"},
            {"name": "A", "highway": "trunk_link"},
            {"name": "A", "highway": "construction"},
            {"name": "A", "highway": "proposed"},
            {"name": "A", "highway": "raceway"},
            {"name": "A", "highway": "residential", "access": "private"},
            {"name": "A", "highway": "residential", "access": "no"},
            {"name": "A", "highway": "residential", "foot": "no"},
            {"name": "A", "highway": "residential", "access": "private", "foot": "yes"},
        ):
            with self.subTest(tags=tags):
                self.assertFalse(eligible_way(tags))

    def test_stitches_reversed_and_out_of_order_relation_members(self):
        relation = Relation(8, {}, (("way", 2, "outer"), ("way", 3, "outer"), ("way", 1, "outer")))
        ways = {
            1: Way(1, (1, 2), {}),
            2: Way(2, (3, 2), {}),
            3: Way(3, (3, 4, 1), {}),
        }
        nodes = {1: (0.0, 0.0), 2: (1.0, 0.0), 3: (1.0, 1.0), 4: (0.0, 1.0)}
        rings = _stitch_rings(relation, ways, nodes)
        self.assertEqual(len(rings["outer"]), 1)
        self.assertEqual(rings["outer"][0][0], rings["outer"][0][-1])
        self.assertEqual(set(rings["outer"][0][:-1]), {1, 2, 3, 4})

    def test_selected_relation_requires_complete_boundary_members(self):
        xml = """<osm version="0.6">
          <node id="1" lon="0" lat="0"/><node id="2" lon="1" lat="0"/>
          <way id="10"><nd ref="1"/><nd ref="2"/><tag k="name" v="edge"/></way>
          <relation id="20"><member type="way" ref="10" role="outer"/>
            <tag k="boundary" v="administrative"/><tag k="admin_level" v="8"/></relation>
        </osm>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tiny.osm"
            path.write_text(xml)
            document = parse_osm_xml(path, {20})
        self.assertEqual(document.relations[20].members, (("way", 10, "outer"),))
        with self.assertRaisesRegex(ValueError, "unclosed outer ring"):
            _stitch_rings(document.relations[20], document.ways, document.nodes)

    def test_rejects_non_way_geometry_member_and_ignores_named_support_members(self):
        nodes = {1: (0.0, 0.0), 2: (1.0, 0.0), 3: (1.0, 1.0), 4: (0.0, 1.0)}
        ways = {1: Way(1, (1, 2, 3, 4, 1), {})}
        allowed = Relation(20, {}, (("node", 9, "admin_centre"), ("relation", 99, "subarea"),
                                    ("way", 1, "outer")))
        self.assertEqual(_stitch_rings(allowed, ways, nodes)["outer"], [(1, 2, 3, 4, 1)])
        unsupported = Relation(20, {}, (("relation", 99, "outer"), ("way", 1, "outer")))
        with self.assertRaisesRegex(ValueError, "non-way"):
            _stitch_rings(unsupported, ways, nodes)

    def test_rejects_invalid_coordinates_and_missing_city_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.osm"
            path.write_text('<osm><node id="1" lon="181" lat="0"/></osm>')
            with self.assertRaisesRegex(ValueError, "WGS84"):
                parse_osm_xml(path, set())
            path.write_text('<osm/>')
            with self.assertRaisesRegex(ValueError, "missing from extract"):
                parse_osm_xml(path, {123})

    def test_physical_duplicate_records_count_towards_streaming_parser_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicates.osm"
            path.write_text('<osm><node id="1" lon="0" lat="0"/><node id="1" lon="0" lat="0"/></osm>')
            with patch("backend.app.geography.MAX_OSM_NODES", 1):
                with self.assertRaisesRegex(ValueError, "osm_node_limit_exceeded"):
                    parse_osm_xml(path, set())
            path.write_text('<osm><way id="1"/><way id="1"/></osm>')
            with patch("backend.app.geography.MAX_OSM_WAYS", 1):
                with self.assertRaisesRegex(ValueError, "osm_way_limit_exceeded"):
                    parse_osm_xml(path, set())
            path.write_text('<osm><way id="1"><nd ref="1"/><nd ref="2"/></way></osm>')
            with patch("backend.app.geography.MAX_OSM_REFERENCES", 1):
                with self.assertRaisesRegex(ValueError, "osm_reference_limit_exceeded"):
                    parse_osm_xml(path, set())

    def test_reference_limit_aborts_inside_a_single_way_before_reading_tail(self):
        document = (b'<osm><way id="1"><nd ref="1"/><nd ref="2"/>'
                    b"</way></osm>" + b" " * 1_000_000)
        source = io.BytesIO(document)
        with patch("backend.app.geography.MAX_OSM_REFERENCES", 1):
            with self.assertRaisesRegex(ValueError, "osm_reference_limit_exceeded"):
                _parse_osm_stream(source, set())
        self.assertLess(source.tell(), len(document) // 2)

    def test_xml_depth_and_total_element_count_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested.osm"
            path.write_text("<osm><node id='1' lon='0' lat='0'><tag><x><y/></x></tag></node></osm>")
            with self.assertRaisesRegex(ValueError, "osm_xml_depth_exceeded"):
                parse_osm_xml(path, set())
            path.write_text("<osm><node id='1' lon='0' lat='0'/><node id='2' lon='0' lat='0'/></osm>")
            with patch("backend.app.geography.MAX_OSM_XML_ELEMENTS", 2):
                with self.assertRaisesRegex(ValueError, "osm_xml_element_limit_exceeded"):
                    parse_osm_xml(path, set())

    def test_snapshot_checksum_is_checked_before_xml_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.osm"
            path.write_bytes(b"not xml")
            with patch("backend.app.geography._parse_osm_stream") as parse:
                with self.assertRaisesRegex(ValueError, "osm_snapshot_checksum_mismatch"):
                    _read_osm_snapshot(path, set(), expected_checksum="0" * 64)
                parse.assert_not_called()

    def test_merges_duplicate_skeleton_way_but_rejects_conflicting_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicates.osm"
            path.write_text('''<osm>
              <way id="7"><nd ref="1"/><nd ref="2"/></way>
              <way id="7"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
            </osm>''')
            document = parse_osm_xml(path, set())
            self.assertEqual(document.ways[7].tags, {"highway": "residential"})
            path.write_text('''<osm>
              <way id="7"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
              <way id="7"><nd ref="1"/><nd ref="2"/><tag k="highway" v="footway"/></way>
            </osm>''')
            with self.assertRaisesRegex(ValueError, "Conflicting duplicate OSM way"):
                parse_osm_xml(path, set())


if __name__ == "__main__":
    unittest.main()
