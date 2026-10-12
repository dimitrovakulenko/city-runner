"""Canonical identity for exactly equivalent segmented GPS recordings."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


RECORDING_IDENTITY_VERSION = 1
MAX_RECORDING_SEGMENTS = 100_000
MAX_RECORDING_POINTS = 100_000
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)\Z"
)


@dataclass(frozen=True)
class RecordingIdentity:
    fingerprint: str
    version: int
    activity_type: str
    tracks: tuple[tuple[tuple[float, float], ...], ...]
    timestamps: tuple[tuple[str, ...], ...]


def _canonical_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    fractional = match.group(1) or ""
    if len(fractional) > 6 and any(digit != "0" for digit in fractional[6:]):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return None


def canonical_recording(tracks: Any, timestamps: Any, activity_type: Any) -> RecordingIdentity | None:
    """Return identity only when complete point/time arrays prove exact equivalence."""
    if (not isinstance(tracks, list) or not tracks or len(tracks) > MAX_RECORDING_SEGMENTS
            or not isinstance(timestamps, list)
            or len(tracks) != len(timestamps) or not isinstance(activity_type, str)):
        return None

    canonical_tracks: list[tuple[tuple[float, float], ...]] = []
    canonical_times: list[tuple[str, ...]] = []
    previous_segment_end: datetime | None = None
    point_count = 0
    for points, times in zip(tracks, timestamps):
        if not isinstance(points, list) or not isinstance(times, list) or len(points) != len(times):
            return None
        if point_count + len(points) > MAX_RECORDING_POINTS:
            return None
        segment_points: list[tuple[float, float]] = []
        segment_times: list[str] = []
        previous_time: datetime | None = None
        for point, raw_time in zip(points, times):
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                           for value in point)):
                return None
            try:
                longitude, latitude = float(point[0]), float(point[1])
            except (OverflowError, ValueError):
                return None
            if (not math.isfinite(longitude) or not math.isfinite(latitude)
                    or not -180 <= longitude <= 180 or not -90 <= latitude <= 90):
                return None
            # JSON and IEEE-754 distinguish -0.0, but recording identity does not.
            longitude = 0.0 if longitude == 0 else longitude
            latitude = 0.0 if latitude == 0 else latitude
            normalized_time = _canonical_time(raw_time)
            if normalized_time is None:
                return None
            instant = datetime.fromisoformat(normalized_time.replace("Z", "+00:00"))
            if previous_time is not None and instant <= previous_time:
                return None
            if previous_time is None and previous_segment_end is not None and instant < previous_segment_end:
                return None
            segment_points.append((longitude, latitude))
            segment_times.append(normalized_time)
            previous_time = instant
            point_count += 1
        if previous_time is not None:
            previous_segment_end = previous_time
        canonical_tracks.append(tuple(segment_points))
        canonical_times.append(tuple(segment_times))

    if point_count < 2:
        return None
    payload = json.dumps(
        {
            "version": RECORDING_IDENTITY_VERSION,
            "activity_type": activity_type,
            "tracks": canonical_tracks,
            "timestamps": canonical_times,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    fingerprint = hashlib.sha256(b"city-runner-recording\0" + payload).hexdigest()
    return RecordingIdentity(fingerprint, RECORDING_IDENTITY_VERSION, activity_type,
                             tuple(canonical_tracks), tuple(canonical_times))


def same_recording(first: RecordingIdentity, second: RecordingIdentity) -> bool:
    """Revalidate the full canonical value after fingerprint candidate lookup."""
    return (first.version == second.version and first.activity_type == second.activity_type
            and first.tracks == second.tracks and first.timestamps == second.timestamps)
