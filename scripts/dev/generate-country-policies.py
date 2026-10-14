#!/usr/bin/env python3
"""Generate a conservative review report from the saved OSM admin-level table."""

from __future__ import annotations

import argparse
import ast
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import unicodedata


ROOT = Path(__file__).resolve().parents[2]
TABLE_CAPTION = "Overview of administrative subdivisions of states"
TABLE_URL = "https://wiki.openstreetmap.org/wiki/Template:Admin_level"
CLDR_URL = "https://github.com/unicode-org/cldr-json/blob/91c267402229a59e3ef2774544f001bf959e8809/cldr-json/cldr-localenames-full/main/en/territories.json"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TABLE_ROWS = 1000
MAX_CELL_CHARS = 20_000
MAX_COLUMNS = 10
ISO_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS
MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS
RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
ALIAS_COUNTRIES = {
    # CLDR's English display names differ from the spelling in this OSM table.
    "cape verde": "CV",
    "ivory coast": "CI",
    "côte d'ivoire": "CI",
    "côte d’ivoire": "CI",
    "sao tome and principe": "ST",
    "são tomé and príncipe": "ST",
    "trinidad & tobago": "TT",
    "trinidad and tobago": "TT",
    "antigua and barbuda": "AG",
    "bosnia and herzegovina": "BA",
    "congo-brazzaville": "CG",
    "holy see (vatican city)": "VA",
    "saint kitts and nevis": "KN",
    "saint lucia": "LC",
    "saint vincent": "VC",
    "turkey": "TR",
    "turkey see nuts as well": "TR",
    "myanmar": "MM",
    "congo-kinshasa": "CD",
}
BASE_PHRASE = re.compile(r"^(?:municipality|municipalities|commune|communes)(?:\s+\(([^()]*)\))?$", re.I)
LAU_PHRASE = re.compile(r"^(?:LAU(?:\s?[12])?|RŠÚJ\s?5|LAU/RŠÚJ\s?5)$", re.I)
LOCAL_TRANSLATIONS = frozenset({
    "bələdiyyələr", "sveitarfélög", "gemeinde", "gemeinden", "gemeenten", "kommune",
    "kommunen", "kommuner", "comuni", "comune", "comunas", "municipio", "municipios",
    "município", "municípios", "bashkia", "bashki", "komunë", "komunat e kosovës",
    "obce", "občine", "gminy", "baladiyah مدينة", "بلدية", "municipalidades",
    "opštine", "општине", "gradovi ili općine", "municipalities (bələdiyyələr)",
})
MUNICIPAL_KEYWORD = re.compile(r"\b(?:municipal(?:ity|ities)?|communes?)\b", re.I)
STATUS_PROPOSED = re.compile(r"\bproposed\b", re.I)


class PolicyInputError(ValueError):
    pass


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture = False
        self.caption = ""
        self._in_caption = False
        self._rows: list[list[dict]] = []
        self._row: list[dict] = []
        self._cell: dict | None = None
        self._segment = ""
        self._skip_depth = 0
        self.tables: list[tuple[str, list[list[dict]]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "table" and self.capture:
            raise PolicyInputError("nested table is unsupported")
        if tag == "table" and not self.capture and "wikitable" in values.get("class", "").split():
            self.capture = True
            self.caption = ""
            self._rows = []
        elif self.capture and tag == "caption":
            self._in_caption = True
        elif self.capture and tag == "tr":
            self._row = []
        elif self.capture and tag in {"td", "th"}:
            self._cell = {"tag": tag, "attrs": values, "segments": [], "link_titles": []}
            self._segment = ""
        elif self.capture and self._cell is not None and tag == "a":
            href = values.get("href", "")
            title = values.get("title", "")
            if (href.startswith("/wiki/") and isinstance(title, str) and title.strip()
                    and not title.casefold().startswith(("flag of ", "key:", "tag:"))):
                self._cell["link_titles"].append(_norm(title))
        elif self.capture and self._cell is not None and tag in {"script", "style"}:
            self._skip_depth += 1
        elif self.capture and self._cell is not None and tag == "br":
            self._flush_segment()

    def handle_data(self, data: str) -> None:
        if self.capture and self._skip_depth == 0:
            if self._in_caption:
                self.caption += data
            elif self._cell is not None:
                self._segment += data
                if len(self._segment) > MAX_CELL_CHARS:
                    raise PolicyInputError("table cell exceeds size limit")

    def handle_endtag(self, tag: str) -> None:
        if self.capture and self._skip_depth and tag in {"script", "style"}:
            self._skip_depth -= 1
        elif self.capture and tag == "caption":
            self._in_caption = False
        elif self.capture and tag in {"td", "th"} and self._cell is not None:
            self._flush_segment()
            self._cell["text"] = " ".join(self._cell["segments"])
            self._row.append(self._cell)
            self._cell = None
        elif self.capture and tag == "tr":
            if self._row:
                self._rows.append(self._row)
                if len(self._rows) > MAX_TABLE_ROWS:
                    raise PolicyInputError("table row limit exceeded")
        elif tag == "table" and self.capture:
            self.tables.append((" ".join(self.caption.split()), self._rows))
            self.capture = False

    def _flush_segment(self) -> None:
        if self._cell is None:
            return
        value = _norm(self._segment)
        if value:
            self._cell["segments"].append(value)
        self._segment = ""


def _norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _span(cell: dict, key: str) -> int:
    raw = cell["attrs"].get(key, "1")
    if not isinstance(raw, str) or re.fullmatch(r"[1-9][0-9]?", raw) is None:
        raise PolicyInputError("invalid table span")
    value = int(raw)
    if value > MAX_COLUMNS:
        raise PolicyInputError("table span exceeds column limit")
    return value


def _expand_grid(rows: list[list[dict]]) -> list[list[dict | None]]:
    active: dict[int, tuple[dict, int]] = {}
    expanded: list[list[dict | None]] = []
    for raw_row in rows:
        grid: list[dict | None] = [None] * MAX_COLUMNS
        next_active: dict[int, tuple[dict, int]] = {}
        for column, (cell, remaining) in active.items():
            grid[column] = cell
            if remaining > 1:
                next_active[column] = (cell, remaining - 1)
        cursor = 0
        for cell in raw_row:
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            while cursor < MAX_COLUMNS and grid[cursor] is not None:
                cursor += 1
            while cursor + colspan <= MAX_COLUMNS and any(grid[cursor + offset] is not None for offset in range(colspan)):
                cursor += 1
            if cursor + colspan > MAX_COLUMNS:
                raise PolicyInputError("table row exceeds expected columns")
            for column in range(cursor, cursor + colspan):
                grid[column] = cell
                if rowspan > 1:
                    next_active[column] = (cell, rowspan - 1)
            cursor += colspan
        expanded.append(grid)
        active = next_active
    if active:
        raise PolicyInputError("table ends inside a rowspan")
    return expanded


def _source_table(raw: bytes) -> tuple[list[list[dict | None]], int | None]:
    if len(raw) > MAX_SOURCE_BYTES:
        raise PolicyInputError("source HTML exceeds size limit")
    try:
        parser = _TableParser()
        parser.feed(raw.decode("utf-8"))
        parser.close()
    except (UnicodeDecodeError, RecursionError) as error:
        raise PolicyInputError("invalid source HTML") from error
    matches = [rows for caption, rows in parser.tables if caption == TABLE_CAPTION]
    if len(matches) != 1:
        raise PolicyInputError("expected exactly one OSM admin-level table")
    grid = _expand_grid(matches[0])
    if len(grid) < 3 or any(cell is None for cell in grid[0]) or any(cell is None for cell in grid[1]):
        raise PolicyInputError("invalid table headers")
    if any(cell is None for row in grid for cell in row):
        raise PolicyInputError("incomplete table row")
    if not grid[0][0] or "country" not in grid[0][0]["text"].casefold():
        raise PolicyInputError("country header not found")
    if [grid[1][column]["text"] for column in range(1, MAX_COLUMNS)] != [str(level) for level in range(3, 12)]:
        raise PolicyInputError("admin-level columns do not match 3 through 11")
    revision_match = re.search(rb"wgRevisionId[\"']?\s*[:=]\s*[\"']?([0-9]+)", raw)
    revision = int(revision_match.group(1)) if revision_match else None
    return grid, revision


def _cldr_names(raw: bytes) -> dict[str, list[str]]:
    if len(raw) > MAX_SOURCE_BYTES:
        raise PolicyInputError("CLDR input exceeds size limit")
    try:
        data = json.loads(raw)
        territories = data["main"]["en"]["localeDisplayNames"]["territories"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, RecursionError) as error:
        raise PolicyInputError("invalid CLDR English territories JSON") from error
    if not isinstance(territories, dict) or len(territories) > 1000:
        raise PolicyInputError("invalid CLDR territory list")
    names: dict[str, list[str]] = {}
    for code, display_name in territories.items():
        if code not in ISO_ALPHA2 or not isinstance(display_name, str) or not display_name.strip():
            continue
        names.setdefault(_norm(display_name).casefold(), []).append(code)
    return names


def _country_match(label: str, link_titles: list[str], names: dict[str, list[str]]) -> tuple[str | None, str, bool, str | None]:
    normalized = _norm(label)
    proposed = bool(STATUS_PROPOSED.search(normalized))
    title_matches = [(code, _norm(title)) for title in link_titles
                     for code in names.get(_norm(title).casefold(), [])]
    title_codes = sorted({code for code, _ in title_matches})
    if len(title_codes) == 1:
        titles = {title.casefold() for code, title in title_matches if code == title_codes[0]}
        clean_scope = any(normalized.casefold() == title or
                          normalized.casefold() == f"{title} (in use)" for title in titles)
        issue = None if clean_scope else "country_label_annotation"
        return title_codes[0], normalized, proposed, issue
    if len(title_codes) > 1:
        return None, normalized, proposed, "ambiguous_cldr_name"
    folded = normalized.casefold()
    if folded == "congo (drc)":
        return None, normalized, proposed, "unmatched_cldr_name"
    if re.fullmatch(r"congo-kinshasa(?: \(drc\))?(?: \(proposed(?: by [a-z0-9 _-]+)?\))?", folded):
        return "CD", normalized, proposed, None
    alias_code = ALIAS_COUNTRIES.get(folded)
    if alias_code:
        return alias_code, normalized, proposed, None
    for alias, code in ALIAS_COUNTRIES.items():
        suffix = folded[len(alias):].strip() if folded.startswith(alias) else ""
        if suffix and _valid_name_suffix(suffix):
            return code, normalized, proposed, None
        if suffix and _parenthetical_commentary(suffix):
            return code, normalized, proposed, "country_label_annotation"
    exact = names.get(folded, [])
    if len(exact) == 1:
        return exact[0], normalized, proposed, None
    if len(exact) > 1:
        return None, normalized, proposed, "ambiguous_cldr_name"
    for name in sorted(names, key=len, reverse=True):
        if not folded.startswith(name):
            continue
        suffix = folded[len(name):].strip()
        if suffix and _valid_name_suffix(suffix):
            codes = names[name]
            if len(codes) == 1:
                return codes[0], normalized, proposed, None
            return None, normalized, proposed, "ambiguous_cldr_name"
        if suffix and _parenthetical_commentary(suffix):
            codes = names[name]
            if len(codes) == 1:
                return codes[0], normalized, proposed, "country_label_annotation"
            return None, normalized, proposed, "ambiguous_cldr_name"
    return None, normalized, proposed, "unmatched_cldr_name"


def _valid_name_suffix(suffix: str) -> bool:
    return bool(re.fullmatch(
        r"(?:proposed(?: by [a-z0-9 .,&'-]+)?|\(proposed(?: by [a-z0-9 .,&'-]+)?\)|"
        r"\(proposed / in use\))",
        suffix, re.I,
    ))


def _parenthetical_commentary(suffix: str) -> bool:
    return bool(re.fullmatch(r"(?:\s*\([^()]*\))+", suffix))


def _municipal_phrase(cell: dict) -> tuple[bool, str | None]:
    segments = cell.get("segments") or [cell.get("text", "")]
    segments = [_norm(segment) for segment in segments if _norm(segment)]
    if not segments:
        return False, None
    first = segments[0]
    if first.casefold() == "borders of municipalities":
        first_ok = True
    else:
        match = BASE_PHRASE.fullmatch(first)
        first_ok = match is not None
        if match and match.group(1):
            first_ok = _norm(match.group(1)).casefold() in LOCAL_TRANSLATIONS
    if not first_ok:
        return False, "cell_phrase_not_exact"
    for segment in segments[1:]:
        if LAU_PHRASE.fullmatch(segment) or _norm(segment).casefold() in LOCAL_TRANSLATIONS:
            continue
        return False, "cell_suffix_not_closed_translation_or_lau"
    return True, None


def _row_cells(grid: list[list[dict | None]]) -> list[dict]:
    result = []
    previous_origin = None
    current = None
    for index, row in enumerate(grid[2:], start=2):
        if row[0] is None:
            continue
        country_cell = row[0]
        if country_cell["text"].casefold().startswith("country "):
            break
        origin = id(country_cell)
        if origin != previous_origin:
            if current:
                result.append(current)
            current = {"label": country_cell["text"], "source_row": index, "rows": []}
            previous_origin = origin
        current["rows"].append(row)
    if current:
        result.append(current)
    return result


def _load_curated() -> tuple[dict[str, list[int]], dict[str, str]]:
    provider_path = ROOT / "backend/app/city_geography_provider.py"
    tree = ast.parse(provider_path.read_text(encoding="utf-8"))
    defaults = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "DEFAULT_ADMIN_LEVELS" for target in targets):
                defaults = ast.literal_eval(node.value)
                break
    if not isinstance(defaults, dict) or any(code not in ISO_ALPHA2 for code in defaults):
        raise PolicyInputError("cannot read curated default policy map")
    sys.path.insert(0, str(ROOT))
    from backend.app.municipal_rules import country_selector_profile
    profiles = {code: country_selector_profile(code) for code in sorted(defaults)}
    return defaults, profiles


def build_report(source: bytes, cldr: bytes) -> tuple[dict, dict[str, list[int]]]:
    grid, revision = _source_table(source)
    names = _cldr_names(cldr)
    curated, profiles = _load_curated()
    groups = _row_cells(grid)
    by_code: dict[str, list[dict]] = {}
    unresolved: list[dict] = []
    records: list[dict] = []

    for group in groups:
        code, label, proposed, country_issue = _country_match(
            group["label"], group["rows"][0][0]["link_titles"], names)
        keyword_evidence: list[dict] = []
        clear_evidence: list[dict] = []
        for row_index, row in enumerate(group["rows"]):
            for offset, cell in enumerate(row[1:], start=1):
                if cell is None:
                    continue
                text = cell["text"]
                if not MUNICIPAL_KEYWORD.search(text):
                    continue
                level = offset + 2
                clear, issue = _municipal_phrase(cell)
                evidence = {"level": level, "segments": cell.get("segments", []), "text": text,
                            "clear_phrase": clear, "issue": issue, "subrow": row_index + 1}
                keyword_evidence.append(evidence)
                if clear:
                    clear_evidence.append(evidence)
        source_levels = sorted({entry["level"] for entry in clear_evidence})
        all_evidence_levels = sorted({entry["level"] for entry in keyword_evidence})
        reasons = []
        if country_issue:
            reasons.append(country_issue)
        if proposed:
            reasons.append("proposed_country_row")
        if len(group["rows"]) != 1:
            reasons.append("multirow_country_entry")
        if not keyword_evidence:
            reasons.append("no_municipal_evidence")
        if any(not entry["clear_phrase"] for entry in keyword_evidence):
            reasons.append("non_simple_municipal_cell")
        if any(entry["level"] > 10 for entry in keyword_evidence):
            reasons.append("unsupported_admin_level")
        if len(source_levels) != 1 or len(keyword_evidence) != 1:
            reasons.append("multiple_municipal_tiers_or_cells")

        if code is None:
            unresolved.append({"country_label": label, "source_row": group["source_row"],
                               "candidate_levels": all_evidence_levels, "evidence": keyword_evidence,
                               "reason": country_issue or "unmatched_cldr_name"})
            continue
        by_code.setdefault(code, []).append({
            "country_name": label, "source_row": group["source_row"], "proposed": proposed,
            "candidate_levels": all_evidence_levels, "clear_levels": source_levels,
            "evidence": keyword_evidence, "reasons": reasons,
        })

    policy_map = {code: sorted(levels) for code, levels in curated.items()}
    for code in sorted(set(by_code) | set(curated)):
        entries = by_code.get(code, [])
        is_curated = code in curated
        group_issues = []
        if len(entries) != 1:
            if entries:
                group_issues.append("duplicate_country_entries")
        entry = entries[0] if entries else {"country_name": None, "source_row": None, "proposed": False,
                                           "candidate_levels": [], "clear_levels": [], "evidence": [], "reasons": []}
        group_issues.extend(entry["reasons"])
        if is_curated:
            source_levels = sorted({level for row in entries for level in row["clear_levels"]})
            drift = bool(entries and source_levels and source_levels != curated[code])
            if drift:
                group_issues.append("curated_source_level_drift")
            elif entries and not source_levels:
                group_issues.append("curated_source_no_clear_level")
            if not entries:
                group_issues.append("curated_source_row_unresolved")
            status = "curated"
        else:
            eligible = not group_issues and len(entry["clear_levels"]) == 1
            status = "candidate" if eligible else "review"
            if eligible:
                policy_map[code] = entry["clear_levels"]
        records.append({
            "country_code": code,
            "country_name": entry["country_name"],
            "status": status,
            "curated_levels": curated.get(code),
            "selector_profile": profiles.get(code),
            "candidate_levels": sorted({level for row in entries for level in row["candidate_levels"]}),
            "source_rows": [{"row": row["source_row"], "proposed": row["proposed"],
                             "candidate_levels": row["candidate_levels"], "evidence": row["evidence"]}
                            for row in entries],
            "review_reasons": sorted(set(group_issues)),
        })

    records.sort(key=lambda row: (row["status"], row["country_code"]))
    report = {
        "schema_version": 1,
        "source": {"url": TABLE_URL, "revision": revision,
                   "sha256": hashlib.sha256(source).hexdigest(), "caption": TABLE_CAPTION},
        "cldr": {"url": CLDR_URL, "sha256": hashlib.sha256(cldr).hexdigest(), "locale": "en"},
        "summary": {
            "source_group_count": len(groups),
            "curated_count": sum(row["status"] == "curated" for row in records),
            "candidate_count": sum(row["status"] == "candidate" for row in records),
            "review_count": sum(row["status"] == "review" for row in records),
            "unresolved_count": len(unresolved),
        },
        "records": records,
        "unresolved": sorted(unresolved, key=lambda row: (row["country_label"].casefold(), row["source_row"])),
    }
    return report, policy_map


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_or_check(path: Path, value, check: bool) -> bool:
    expected = _json_bytes(value)
    if check:
        try:
            return path.read_bytes() == expected
        except OSError:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)
    return True


def _read_input(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise PolicyInputError(f"input exceeds size limit: {path.name}")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--countries", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--policies-output", type=Path)
    parser.add_argument("--check", action="store_true", help="compare generated output with existing files")
    args = parser.parse_args(argv)
    try:
        input_paths = {args.source.resolve(), args.countries.resolve()}
        output_paths = [args.output, *([args.policies_output] if args.policies_output else [])]
        resolved_outputs = [path.resolve() for path in output_paths]
        if any(path in input_paths for path in resolved_outputs) or len(set(resolved_outputs)) != len(resolved_outputs):
            raise PolicyInputError("output paths must be distinct from inputs and each other")
        report, policy_map = build_report(_read_input(args.source), _read_input(args.countries))
        ok = _write_or_check(args.output, report, args.check)
        if args.policies_output:
            ok = _write_or_check(args.policies_output, policy_map, args.check) and ok
    except (OSError, PolicyInputError, SyntaxError, ValueError) as error:
        print(f"country policy generation failed: {error}", file=sys.stderr)
        return 2
    if not ok:
        print("generated output differs from checked-in output", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
