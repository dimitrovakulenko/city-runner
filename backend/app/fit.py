"""Bounded FIT activity parsing into the existing segmented activity shape."""

from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone

import fitdecode

from backend.app.gpx import GpxError, MAX_GPX_BYTES, MAX_GPX_POINTS, ParsedGpx


FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SEMICIRCLES_TO_DEGREES = 180.0 / (1 << 31)


MAX_FIT_MESSAGES = 200_000


def _field(message, name: str):
    for field in message.fields:
        if field.name_or_num == name and (field.field_def is None or not field.field_def.is_dev):
            return field
    return None


def _value(message, name: str):
    field = _field(message, name)
    return field.value if field is not None else None


def _raw_value(message, name: str):
    field = _field(message, name)
    return field.raw_value if field is not None else None

def _enum(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower()
    return None


def _timestamp(value) -> str | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0x10000000:
        try:
            return (FIT_EPOCH + timedelta(seconds=value)).isoformat().replace("+00:00", "Z")
        except (OverflowError, ValueError):
            return None
    return None


def _coordinate(value, limit: float) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or value == 0x7FFFFFFF:
        return None
    degrees = value * SEMICIRCLES_TO_DEGREES
    if not -limit <= degrees <= limit:
        return None
    return degrees


def parse_fit(content: bytes, *, max_bytes: int = MAX_GPX_BYTES,
              max_records: int = MAX_GPX_POINTS, max_messages: int = MAX_FIT_MESSAGES) -> ParsedGpx:
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise GpxError("invalid_fit_size")

    try:
        frames = fitdecode.FitReader(
            io.BytesIO(content), check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.RAISE,
        )
        headers = 0
        file_type = None
        records = 0
        messages = 0
        segments: list[list[list[float]]] = []
        timestamp_segments: list[list[str | None]] = []
        current_points: list[list[float]] = []
        current_times: list[str | None] = []
        first_timestamp = None
        activity_type = "unknown"
        timer_seen = False
        timer_running = True

        def flush() -> None:
            nonlocal current_points, current_times
            if current_points:
                segments.append(current_points)
                timestamp_segments.append(current_times)
            current_points, current_times = [], []

        with frames:
            for frame in frames:
                if frame.frame_type == fitdecode.FIT_FRAME_HEADER:
                    headers += 1
                    if headers > 1:
                        raise GpxError("fit_multiple_streams")
                    continue
                if frame.frame_type in (fitdecode.FIT_FRAME_DATA, fitdecode.FIT_FRAME_DEFINITION):
                    messages += 1
                    if messages > max_messages:
                        raise GpxError("fit_message_limit")
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name == "file_id":
                    kind = _value(frame, "type")
                    file_type = kind
                    if kind not in (4, "activity"):
                        raise GpxError("fit_not_activity")
                elif frame.name == "session":
                    sport = _enum(_value(frame, "sport"))
                    if sport and activity_type == "unknown":
                        activity_type = sport[:80]
                elif frame.name == "event":
                    event = _enum(_value(frame, "event"))
                    event_type = _enum(_value(frame, "event_type"))
                    if event == "timer" and event_type in {
                        "stop", "stop_all", "stop_disable", "stop_disable_all",
                    }:
                        flush()
                        timer_seen = True
                        timer_running = False
                    elif event == "timer" and event_type == "start":
                        flush()
                        timer_seen = True
                        timer_running = True
                elif frame.name == "record":
                    records += 1
                    if records > max_records:
                        raise GpxError("fit_record_limit")
                    if timer_seen and not timer_running:
                        continue
                    latitude = _coordinate(_raw_value(frame, "position_lat"), 90)
                    longitude = _coordinate(_raw_value(frame, "position_long"), 180)
                    timestamp = _timestamp(_value(frame, "timestamp"))
                    if latitude is None or longitude is None:
                        flush()
                        continue
                    current_points.append([longitude, latitude])
                    current_times.append(timestamp)
                    first_timestamp = first_timestamp or timestamp
        if headers != 1 or file_type is None:
            raise GpxError("invalid_fit_file")
        flush()
        if not segments:
            raise GpxError("fit_no_track_points")
        date = first_timestamp[:10] if first_timestamp else "unknown"
        return ParsedGpx(
            name="Unknown activity", activity_type=activity_type, date=date,
            tracks=segments, timestamps=timestamp_segments, point_count=sum(map(len, segments)),
        )
    except GpxError:
        raise
    except Exception as exc:
        # Parser details may contain file data; expose only a stable error code.
        raise GpxError("invalid_fit_file") from exc
