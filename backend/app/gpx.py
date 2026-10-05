"""Bounded GPX parsing that preserves segments and point-aligned timestamps."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException


MAX_GPX_BYTES = 10 * 1024 * 1024
MAX_GPX_POINTS = 100_000
_FULL_ZONED_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)\Z"
)


class GpxError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ParsedGpx:
    name: str
    activity_type: str
    date: str
    tracks: list[list[list[float]]]
    timestamps: list[list[str | None]]
    point_count: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            value = (child.text or "").strip()
            if value:
                return value
    return None


def _timestamp(value: str | None) -> str | None:
    if value is None or not _FULL_ZONED_TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


def _point(element) -> tuple[list[float], str | None]:
    try:
        latitude = float(element.attrib["lat"])
        longitude = float(element.attrib["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GpxError("invalid_gpx_coordinate") from exc
    if (not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        raise GpxError("invalid_gpx_coordinate")
    return [longitude, latitude], _timestamp(_child_text(element, "time"))


def parse_gpx(content: bytes, *, max_bytes: int = MAX_GPX_BYTES,
              max_points: int = MAX_GPX_POINTS) -> ParsedGpx:
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise GpxError("invalid_gpx_size")
    try:
        root = ElementTree.fromstring(content, forbid_dtd=True)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise GpxError("invalid_gpx_xml") from exc
    if _local_name(root.tag) != "gpx":
        raise GpxError("invalid_gpx_root")

    metadata_name = None
    for child in root:
        if _local_name(child.tag) == "metadata":
            metadata_name = _child_text(child, "name")
            break
    segments: list[list[list[float]]] = []
    timestamp_segments: list[list[str | None]] = []
    track_name = None
    track_type = None
    point_count = 0

    for track in (child for child in root if _local_name(child.tag) == "trk"):
        track_name = track_name or _child_text(track, "name")
        track_type = track_type or _child_text(track, "type")
        for track_segment in (child for child in track if _local_name(child.tag) == "trkseg"):
            points: list[list[float]] = []
            timestamps: list[str | None] = []
            for element in track_segment:
                if _local_name(element.tag) != "trkpt":
                    continue
                point_count += 1
                if point_count > max_points:
                    raise GpxError("gpx_point_limit")
                coordinate, timestamp = _point(element)
                points.append(coordinate)
                timestamps.append(timestamp)
            if points:
                segments.append(points)
                timestamp_segments.append(timestamps)

    if not point_count:
        raise GpxError("gpx_no_track_points")
    first_timestamp = next((value for segment in timestamp_segments for value in segment if value), None)
    date = first_timestamp[:10] if first_timestamp else "unknown"
    return ParsedGpx(
        name=(metadata_name or track_name or "Unknown activity")[:200],
        activity_type=(track_type.strip().lower() if track_type else "unknown")[:80],
        date=date,
        tracks=segments,
        timestamps=timestamp_segments,
        point_count=point_count,
    )
