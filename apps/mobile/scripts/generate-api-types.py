#!/usr/bin/env python3
"""Generate the small mobile API type surface from FastAPI's OpenAPI schema."""

from __future__ import annotations

import sys
import argparse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parents[1] / "src" / "api" / "generated.ts"
sys.path.insert(0, str(ROOT))

from backend.app.main import app  # noqa: E402


SCHEMAS = (
    "AccountResponse",
    "ActivitySummary",
    "ActivityPage",
    "ActivityDetail",
    "ChallengeRequest",
    "ChallengeResponse",
    "ExchangeRequest",
    "ExchangeResponse",
    "MeResponse",
    "MapResponse",
    "ProgressResponse",
    "UploadResponse",
    "UploadStatusResponse",
    "CityPage",
    "StreetPage",
    "StreetDetail",
    "ContributionPage",
    "ManualCompletionBody",
    "ImportBatchCreate",
    "ImportBatchResponse",
    "ImportBatchPage",
    "RoutePreviewRequest",
    "RoutePreviewResponse",
    "RouteCreateRequest",
    "RouteUpdateRequest",
    "RouteDetail",
    "RouteSummary",
    "RoutePage",
    "RouteGeometry",
    "RouteAttribution",
)


def render(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return repr(schema["const"])
    if "enum" in schema:
        return " | ".join(repr(value) for value in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(dict.fromkeys(render(part) for part in schema["anyOf"]))
    if "prefixItems" in schema:
        return "[" + ", ".join(render(part) for part in schema["prefixItems"]) + "]"
    if schema.get("type") == "array":
        return f"Array<{render(schema.get('items', {}))}>"
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if properties is None:
            return "Record<string, unknown>"
        required = set(schema.get("required", []))
        fields = [
            f"  {name}{'' if name in required else '?'}: {render(value)};"
            for name, value in properties.items()
        ]
        return "{\n" + "\n".join(fields) + "\n}"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(schema.get("type"), "unknown")


components = app.openapi()["components"]["schemas"]
missing = [name for name in SCHEMAS if name not in components]
if missing:
    raise SystemExit(f"OpenAPI is missing expected schemas: {', '.join(missing)}")


def dependencies(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {value["$ref"].rsplit("/", 1)[-1]} if "$ref" in value else set()
        return names | set().union(*(dependencies(part) for part in value.values()))
    if isinstance(value, list):
        return set().union(*(dependencies(part) for part in value))
    return set()


schema_names = set(SCHEMAS)
pending = list(SCHEMAS)
while pending:
    for name in dependencies(components[pending.pop()]):
        if name not in schema_names:
            schema_names.add(name)
            pending.append(name)

definitions = [
    f"export type {name} = {render(components[name])};"
    for name in (*SCHEMAS, *sorted(schema_names - set(SCHEMAS)))
]
generated = (
    "// Generated from backend/app/main.py OpenAPI. Do not edit by hand.\n\n"
    + "\n\n".join(definitions)
    + "\n"
)
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true", help="fail if generated types are out of date")
args = parser.parse_args()
if args.check:
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != generated:
        raise SystemExit(f"{OUTPUT.relative_to(ROOT)} is stale; rerun the generator")
    print(f"Checked {OUTPUT.relative_to(ROOT)}")
else:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"Generated {OUTPUT.relative_to(ROOT)}")
