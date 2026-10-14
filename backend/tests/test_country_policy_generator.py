from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/generate-country-policies.py"
SPEC = importlib.util.spec_from_file_location("country_policy_generator", SCRIPT)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def cell(text: str, *, tag: str = "td", rowspan: int = 1, colspan: int = 1) -> str:
    attrs = "" if rowspan == colspan == 1 else f' rowspan="{rowspan}" colspan="{colspan}"'
    return f"<{tag}{attrs}>{text}</{tag}>"


def table(rows: list[str]) -> bytes:
    header = (
        "<tr>" + cell("Country", tag="th", rowspan=2) + cell("admin_level=*", tag="th", colspan=9) + "</tr>"
        "<tr>" + "".join(cell(str(level), tag="th") for level in range(3, 12)) + "</tr>"
    )
    return ("<html><table class='wikitable'><caption>" + generator.TABLE_CAPTION + "</caption>"
            "<tbody>" + header + "".join(rows) + "</tbody></table></html>").encode()


def data_row(name: str, level: int, description: str, *, rowspan: int = 1, title: str | None = None) -> str:
    country = f'<a href="/wiki/{title or name}" title="{title or name}">{name}</a>'
    cells = [cell(country, rowspan=rowspan)]
    cells.extend(cell("—") for _ in range(level - 3))
    cells.append(cell(description))
    cells.extend(cell("—") for _ in range(11 - level))
    return "<tr>" + "".join(cells) + "</tr>"


def territories(*items: tuple[str, str]) -> bytes:
    return json.dumps({"main": {"en": {"localeDisplayNames": {"territories": dict(items)}}}}).encode()


class CountryPolicyGeneratorTests(unittest.TestCase):
    def test_grid_expands_header_rowspans_and_country_colspans_with_br_segments(self):
        source = table([data_row("Andorra", 8, "Municipalities<br/>LAU", title="Andorra")]).replace(
            b"<html>", b'<html><script>"wgRevisionId":"3104571"</script>')
        grid, revision = generator._source_table(source)
        self.assertEqual(revision, 3104571)
        self.assertEqual([grid[1][index]["text"] for index in range(1, 10)], [str(x) for x in range(3, 12)])
        self.assertEqual(grid[2][6]["segments"], ["Municipalities", "LAU"])

    def test_only_single_clear_nonproposed_municipal_row_becomes_candidate(self):
        source = table([
            data_row("Andorra", 8, "Municipalities<br/>LAU", title="Andorra"),
            data_row("Benin (proposed)", 6, "Commune", title="Benin"),
            data_row("Congo", 6, "Municipality", rowspan=2, title="Congo"),
            "<tr>" + "".join(cell("—") for _ in range(9)) + "</tr>",
            data_row("Côte d’Ivoire", 8, "Municipalities<br/>Cities and districts", title="Côte d’Ivoire"),
        ])
        cldr = territories(("AD", "Andorra"), ("BJ", "Benin"), ("CG", "Congo"), ("CI", "Côte d’Ivoire"))
        report, policy_map = generator.build_report(source, cldr)
        statuses = {row["country_code"]: row["status"] for row in report["records"] if row["country_code"] in {"AD", "BJ", "CG", "CI"}}
        self.assertEqual(statuses, {"AD": "candidate", "BJ": "review", "CG": "review", "CI": "review"})
        self.assertEqual(policy_map["AD"], [8])
        self.assertNotIn("BJ", policy_map)
        self.assertIn("proposed_country_row", next(row for row in report["records"] if row["country_code"] == "BJ")["review_reasons"])
        self.assertIn("multirow_country_entry", next(row for row in report["records"] if row["country_code"] == "CG")["review_reasons"])
        self.assertIn("non_simple_municipal_cell", next(row for row in report["records"] if row["country_code"] == "CI")["review_reasons"])

    def test_curated_overlay_is_preserved_even_when_its_table_row_is_unresolved(self):
        source = table([data_row("new levels:<br/>Germany", 6, "Municipalities", title="Germany")])
        cldr = territories(("DE", "Germany"))
        report, policy_map = generator.build_report(source, cldr)
        curated, profiles = generator._load_curated()
        self.assertEqual(policy_map, {**curated, "DE": curated["DE"]})
        self.assertEqual(len([row for row in report["records"] if row["status"] == "curated"]), len(curated))
        germany = next(row for row in report["records"] if row["country_code"] == "DE")
        self.assertEqual(germany["selector_profile"], profiles["DE"])
        self.assertEqual(germany["candidate_levels"], [6])
        self.assertIn("curated_source_level_drift", germany["review_reasons"])

    def test_curated_non_simple_evidence_is_not_reported_as_level_drift(self):
        source = table([data_row("Germany", 8, "Municipalities / cities", title="Germany")])
        report, _ = generator.build_report(source, territories(("DE", "Germany")))
        germany = next(row for row in report["records"] if row["country_code"] == "DE")
        self.assertIn("curated_source_no_clear_level", germany["review_reasons"])
        self.assertNotIn("curated_source_level_drift", germany["review_reasons"])

    def test_multiple_clear_levels_and_unmatched_iso_names_are_reported_not_activated(self):
        source = table([
            data_row("Andorra", 6, "Commune"),
            data_row("Andorra", 8, "Municipality"),
            data_row("Mysterious island", 8, "Municipalities"),
        ])
        cldr = territories(("AD", "Andorra"))
        report, policy_map = generator.build_report(source, cldr)
        andorra = next(row for row in report["records"] if row["country_code"] == "AD")
        self.assertEqual(andorra["status"], "review")
        self.assertIn("duplicate_country_entries", andorra["review_reasons"])
        self.assertNotIn("Mysterious island", {row["country_name"] for row in report["records"]})
        self.assertEqual(report["unresolved"][0]["country_label"], "Mysterious island")
        self.assertNotIn("ZZ", policy_map)

    def test_reserved_cldr_codes_are_never_automatic_candidates(self):
        source = table([
            data_row("Europe", 8, "Municipalities", title="Europe"),
            data_row("Pseudo Accents", 8, "Municipalities", title="Pseudo Accents"),
        ])
        cldr = territories(("EU", "Europe"), ("XA", "Pseudo Accents"))
        report, policy_map = generator.build_report(source, cldr)
        self.assertNotIn("EU", policy_map)
        self.assertNotIn("XA", policy_map)
        self.assertEqual(report["summary"]["unresolved_count"], 2)
        self.assertEqual({item["reason"] for item in report["unresolved"]}, {"unmatched_cldr_name"})
        resolved_groups = sum(len(row["source_rows"]) for row in report["records"])
        self.assertEqual(resolved_groups + len(report["unresolved"]), report["summary"]["source_group_count"])

    def test_country_aliases_are_closed_and_prefixes_do_not_guess(self):
        names = {"congo": ["CG"]}
        self.assertEqual(generator._country_match("Côte d'Ivoire", [], {})[0], "CI")
        self.assertEqual(generator._country_match("Côte d’Ivoire", [], {})[0], "CI")
        self.assertEqual(generator._country_match("Sao Tome and Principe", [], {})[0], "ST")
        self.assertEqual(generator._country_match("Trinidad & Tobago", [], {})[0], "TT")
        self.assertEqual(generator._country_match("Congo-Kinshasa (DRC)", [], names)[0], "CD")
        self.assertEqual(generator._country_match("Congo-Brazzaville proposed", [], {})[0], "CG")
        self.assertIsNone(generator._country_match("Congo (DRC)", [], names)[0])
        self.assertEqual(generator._country_match("Andorra (proposed)", [], {"andorra": ["AD"]})[:3],
                         ("AD", "Andorra (proposed)", True))
        annotated = generator._country_match("Andorra (see details)", [], {"andorra": ["AD"]})
        self.assertEqual(annotated[0], "AD")
        self.assertEqual(annotated[3], "country_label_annotation")

    def test_linked_country_with_scope_annotation_is_review_not_candidate(self):
        report, policy_map = generator.build_report(
            table([data_row("Andorra (only some areas)", 8, "Municipalities", title="Andorra")]),
            territories(("AD", "Andorra")),
        )
        record = next(row for row in report["records"] if row["country_code"] == "AD")
        self.assertEqual(record["status"], "review")
        self.assertIn("country_label_annotation", record["review_reasons"])
        self.assertNotIn("AD", policy_map)

    def test_parenthetical_commentary_maps_only_an_exact_base_country_name(self):
        netherlands_label = (
            "Netherlands (see administrative divisions of the Kingdom and administrative divisions of the Netherlands)"
        )
        myanmar_label = "Myanmar (proposed) (see Administrative divisions of Myanmar)"
        report, policy_map = generator.build_report(
            table([data_row(netherlands_label, 8, "Municipalities"),
                   data_row(myanmar_label, 8, "Municipalities")]),
            territories(("NL", "Netherlands")),
        )
        records = {row["country_code"]: row for row in report["records"]}
        self.assertIn("NL", records)
        self.assertIn("MM", records)
        self.assertIn("country_label_annotation", records["NL"]["review_reasons"])
        self.assertIn("country_label_annotation", records["MM"]["review_reasons"])
        self.assertNotIn("MM", policy_map)

    def test_level_11_evidence_is_review_only(self):
        report, policy_map = generator.build_report(
            table([data_row("Andorra", 11, "Municipalities")]), territories(("AD", "Andorra")))
        record = next(row for row in report["records"] if row["country_code"] == "AD")
        self.assertEqual(record["status"], "review")
        self.assertIn("unsupported_admin_level", record["review_reasons"])
        self.assertNotIn("AD", policy_map)

    def test_report_keeps_resolved_country_rows_without_municipal_evidence(self):
        source = table([data_row("Andorra", 8, "Parishes")])
        report, policy_map = generator.build_report(source, territories(("AD", "Andorra")))
        record = next(row for row in report["records"] if row["country_code"] == "AD")
        self.assertEqual(record["country_name"], "Andorra")
        self.assertEqual(record["status"], "review")
        self.assertIn("no_municipal_evidence", record["review_reasons"])
        self.assertNotIn("AD", policy_map)

    def test_safe_suffix_lexicon_rejects_mixed_or_conditional_phrases(self):
        accepted = {
            "Municipalities", "Commune (bələdiyyələr)", "Borders of municipalities",
            "Municipalities<br/>LAU2", "Municipalities<br/>LAU/RŠÚJ 5",
        }
        rejected = {
            "Municipality of Bucharest", "Commune (except Lomé)", "Municipalities<br/>Cities / districts",
            "Municipalities?", "Municipalities<br/>former", "Municipalities<br/>disputed",
            "Municipalities<br/>unknowntranslation",
        }
        for value in accepted:
            segments = [part.strip() for part in value.split("<br/>")]
            is_clear, reason = generator._municipal_phrase({"text": " ".join(segments), "segments": segments})
            self.assertTrue(is_clear, (value, reason))
        for value in rejected:
            segments = [part.strip() for part in value.split("<br/>")]
            is_clear, _ = generator._municipal_phrase({"text": " ".join(segments), "segments": segments})
            self.assertFalse(is_clear, value)

    def test_span_limits_and_malformed_table_fail_closed(self):
        with self.assertRaises(generator.PolicyInputError):
            generator._source_table(table(["<tr><td rowspan='0'>Bad</td></tr>"]))
        with self.assertRaises(generator.PolicyInputError):
            generator._source_table(b"<table class='wikitable'><caption>wrong</caption></table>")
        with self.assertRaises(generator.PolicyInputError):
            generator._source_table(table(["<tr><td>Andorra<table><tr><td>nested</td></tr></table></td></tr>"]))
        with self.assertRaises(generator.PolicyInputError):
            generator._source_table(table(["<tr><td>Andorra</td><td>Municipalities</td></tr>"]))
        with self.assertRaises(generator.PolicyInputError):
            generator._cldr_names(b"\xff")

    def test_cli_rejects_output_alias_and_bounded_input_read(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.html"
            countries = Path(folder) / "countries.json"
            original = table([data_row("Andorra", 8, "Municipalities")])
            source.write_bytes(original)
            countries.write_bytes(territories(("AD", "Andorra")))
            self.assertEqual(generator.main(["--source", str(source), "--countries", str(countries),
                                             "--output", str(source)]), 2)
            self.assertEqual(source.read_bytes(), original)
            oversized = Path(folder) / "oversized"
            oversized.write_bytes(b"12345")
            original_limit = generator.MAX_SOURCE_BYTES
            try:
                generator.MAX_SOURCE_BYTES = 4
                with self.assertRaises(generator.PolicyInputError):
                    generator._read_input(oversized)
            finally:
                generator.MAX_SOURCE_BYTES = original_limit

    def test_output_check_is_byte_deterministic(self):
        value = {"alpha": [1], "beta": "é"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            self.assertTrue(generator._write_or_check(path, value, False))
            self.assertTrue(generator._write_or_check(path, value, True))
            path.write_text("{}\n", encoding="utf-8")
            self.assertFalse(generator._write_or_check(path, value, True))


if __name__ == "__main__":
    unittest.main()
