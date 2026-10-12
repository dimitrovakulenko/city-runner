"""Explicit bounded identity backfill for legacy activities."""

from __future__ import annotations

from sqlalchemy import Engine, text

from backend.app.owner_guard import owner_lock
from backend.app.recording_groups import activity_recording_identity


MIN_BIGINT = -(1 << 63)
MAX_BIGINT = (1 << 63) - 1


def backfill_recording_identities(engine: Engine, account_id: str, *, after_id: int | None = None,
                                  batch_size: int = 20, max_activities: int = 100) -> dict:
    if (not isinstance(account_id, str) or not account_id or len(account_id) > 255
            or (after_id is not None and (not isinstance(after_id, int) or isinstance(after_id, bool)
                                          or not MIN_BIGINT <= after_id <= MAX_BIGINT))
            or not isinstance(batch_size, int) or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 20 or not isinstance(max_activities, int)
            or isinstance(max_activities, bool) or not 1 <= max_activities <= 100):
        raise ValueError("invalid recording backfill bounds")

    cursor = after_id
    processed = ineligible = conflicts = examined = 0
    while examined < max_activities:
        limit = min(batch_size, max_activities - examined)
        with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
            with db.begin():
                owner_lock(db, account_id)
                ids = db.execute(text("""SELECT id FROM activities
                    WHERE user_id=:account AND recording_fingerprint_version=0
                      AND (CAST(:after_id AS BIGINT) IS NULL OR id>CAST(:after_id AS BIGINT))
                    ORDER BY id LIMIT :limit"""), {
                    "account": account_id, "after_id": cursor, "limit": limit,
                }).scalars().all()
        if not ids:
            break
        for activity_id in ids:
            with engine.connect().execution_options(isolation_level="READ COMMITTED") as db:
                with db.begin():
                    owner_lock(db, account_id, exclusive=True)
                    row = db.execute(text("""SELECT id,activity_type,tracks,timestamps,
                            recording_fingerprint,recording_fingerprint_version
                        FROM activities WHERE user_id=:account AND id=:id FOR UPDATE"""), {
                        "account": account_id, "id": activity_id,
                    }).mappings().first()
                    if row is not None and int(row["recording_fingerprint_version"]) == 0:
                        identity = activity_recording_identity(row)
                        if identity is not None:
                            candidates = db.execute(text("""SELECT id
                                FROM activities WHERE user_id=:account AND id<>:id
                                  AND recording_fingerprint=:fingerprint
                                  AND recording_fingerprint_version=:version LIMIT 2"""), {
                                "account": account_id, "id": activity_id,
                                "fingerprint": identity.fingerprint, "version": identity.version,
                            }).mappings().all()
                            if candidates:
                                conflicts += 1
                        if identity is None:
                            ineligible += 1
                        fingerprint = identity.fingerprint if identity is not None and not candidates else None
                        version = identity.version if identity is not None else 1
                        db.execute(text("""UPDATE activities SET recording_fingerprint=:fingerprint,
                                recording_fingerprint_version=:version
                            WHERE user_id=:account AND id=:id AND recording_fingerprint_version=0"""), {
                            "fingerprint": fingerprint, "version": version,
                            "account": account_id, "id": activity_id,
                        })
                        processed += 1
            cursor = int(activity_id)
            examined += 1
            if examined >= max_activities:
                break
    return {"processed": processed, "ineligible": ineligible, "conflicts": conflicts,
            "after_id": cursor}
