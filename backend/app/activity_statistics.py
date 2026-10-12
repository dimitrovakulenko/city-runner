"""Deterministic, bounded statistics from the stored segmented GPS samples."""

from __future__ import annotations

import math
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, text

from backend.app.owner_guard import owner_lock


ACTIVITY_STATISTICS_VERSION = 1
MAX_ACTIVITY_POINTS = 100_000
MAX_ACTIVITY_SEGMENTS = 100_000
EARTH_RADIUS_M = 6_371_000.0
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)\Z"
)


@dataclass(frozen=True)
class ActivityStatistics:
    distance_m: float | None
    duration_s: float | None
    pace_s_per_km: float | None
    version: int = ACTIVITY_STATISTICS_VERSION


def _position(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lon, lat = value
    if (not isinstance(lon, (int, float)) or isinstance(lon, bool)
            or not isinstance(lat, (int, float)) or isinstance(lat, bool)):
        return None
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    return float(lon), float(lat)


def _distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1 = math.radians(first[1])
    lat2 = math.radians(second[1])
    dlat = lat2 - lat1
    dlon = math.radians(math.remainder(second[0] - first[0], 360.0))
    haversine = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    haversine = min(1.0, max(0.0, haversine))
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(haversine))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def calculate_activity_statistics(tracks: Any, timestamps: Any) -> ActivityStatistics:
    """Return versioned stats; malformed GPS makes every statistic unavailable."""
    unavailable = ActivityStatistics(None, None, None)
    if not isinstance(tracks, list) or len(tracks) > MAX_ACTIVITY_SEGMENTS:
        return unavailable

    point_count = 0
    parsed_segments: list[list[tuple[float, float]]] = []
    distances: list[float] = []
    has_edge = False
    for segment in tracks:
        if not isinstance(segment, list):
            return unavailable
        point_count += len(segment)
        if point_count > MAX_ACTIVITY_POINTS:
            return unavailable
        positions: list[tuple[float, float]] = []
        for point in segment:
            position = _position(point)
            if position is None:
                return unavailable
            positions.append(position)
        for first, second in zip(positions, positions[1:]):
            has_edge = True
            distances.append(_distance_m(first, second))
        parsed_segments.append(positions)

    distance = math.fsum(distances) if has_edge else None
    if distance is not None and not math.isfinite(distance):
        return unavailable

    duration = None
    timing_shape_valid = (isinstance(timestamps, list) and len(timestamps) == len(parsed_segments)
                          and all(isinstance(segment, list) and len(segment) == len(points)
                                  for segment, points in zip(timestamps, parsed_segments)))
    if timing_shape_valid and point_count > 1:
        elapsed: list[float] = []
        previous_segment_end: datetime | None = None
        timing_valid = True
        for segment in timestamps:
            values = [_timestamp(value) for value in segment]
            if any(value is None for value in values):
                timing_valid = False
                break
            parsed_times = [value for value in values if value is not None]
            if any(current <= previous for previous, current in zip(parsed_times, parsed_times[1:])):
                timing_valid = False
                break
            if parsed_times and previous_segment_end is not None and parsed_times[0] < previous_segment_end:
                timing_valid = False
                break
            elapsed.extend((current - previous).total_seconds()
                           for previous, current in zip(parsed_times, parsed_times[1:]))
            if parsed_times:
                previous_segment_end = parsed_times[-1]
        if timing_valid and elapsed:
            candidate = math.fsum(elapsed)
            if math.isfinite(candidate) and candidate > 0:
                duration = candidate

    pace = (duration * 1000.0 / distance
            if duration is not None and distance is not None and distance > 0 else None)
    if pace is not None and not math.isfinite(pace):
        pace = None
    return ActivityStatistics(distance, duration, pace)


def backfill_activity_statistics(engine: Engine, account_id: str, *, after_id: int | None = None,
                                 batch_size: int = 20, max_activities: int = 100) -> dict[str, int | None]:
    """Backfill one bounded owner page, committing each locked activity separately."""
    if not isinstance(account_id, str) or not account_id or len(account_id) > 255:
        raise ValueError("account_id is required")
    if (after_id is not None and (not isinstance(after_id, int) or isinstance(after_id, bool)
            or not -9_223_372_036_854_775_808 <= after_id <= 9_223_372_036_854_775_807)):
        raise ValueError("after_id must be a signed BIGINT")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 20:
        raise ValueError("batch_size must be between 1 and 20")
    if not isinstance(max_activities, int) or isinstance(max_activities, bool) or not 1 <= max_activities <= 100:
        raise ValueError("max_activities must be between 1 and 100")

    cursor = after_id
    examined = processed = unavailable = 0
    while examined < max_activities:
        size = min(batch_size, max_activities - examined)
        with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
            with db.begin():
                _backfill_timeouts(db)
                owner_lock(db, account_id)
                ids = db.execute(text("""SELECT id FROM activities
                    WHERE user_id=:account AND statistics_version=0
                      AND (CAST(:after_id AS BIGINT) IS NULL OR id>CAST(:after_id AS BIGINT))
                    ORDER BY id LIMIT :limit"""), {
                    "account": account_id, "after_id": cursor, "limit": size,
                }).scalars().all()
        if not ids:
            break

        for activity_id in ids:
            with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
                with db.begin():
                    _backfill_timeouts(db)
                    owner_lock(db, account_id)
                    row = db.execute(text("""SELECT tracks,timestamps,statistics_version
                        FROM activities WHERE id=:id AND user_id=:account FOR UPDATE"""), {
                        "id": activity_id, "account": account_id,
                    }).mappings().first()
                    if row is not None and int(row["statistics_version"]) == 0:
                        tracks = _decode_json(row["tracks"])
                        timestamps = _decode_json(row["timestamps"])
                        stats = calculate_activity_statistics(tracks, timestamps)
                        updated = db.execute(text("""UPDATE activities SET distance_m=:distance,
                                duration_s=:duration,pace_s_per_km=:pace,statistics_version=:version
                            WHERE id=:id AND user_id=:account AND statistics_version=0"""), {
                            "distance": stats.distance_m, "duration": stats.duration_s,
                            "pace": stats.pace_s_per_km, "version": stats.version,
                            "id": activity_id, "account": account_id,
                        })
                        if updated.rowcount != 1:
                            raise RuntimeError("activity changed while locked")
                        processed += 1
                        if stats.distance_m is None and stats.duration_s is None and stats.pace_s_per_km is None:
                            unavailable += 1
            # Advance only after this candidate's transaction commits. A timeout
            # or failure exits before returning a cursor past the failed row.
            cursor = int(activity_id)
            examined += 1
            if examined >= max_activities:
                break
    return {"processed": processed, "unavailable": unavailable, "after_id": cursor}


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return None
    return value


def _backfill_timeouts(db) -> None:
    if db.dialect.name == "postgresql":
        db.execute(text("""SELECT set_config('lock_timeout','250ms',true),
            set_config('statement_timeout','3000ms',true)""")).all()
