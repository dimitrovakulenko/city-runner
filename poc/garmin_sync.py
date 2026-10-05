"""Personal-account experiment, not the approved Garmin partner API."""

import argparse
import getpass
import logging
import time

from garminconnect import Garmin

from poc.core import Overpass, Store

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)


def token_directory(store):
    path = store.directory / "garmin"
    path.mkdir(exist_ok=True, mode=0o700)
    return path


def login(store):
    client = Garmin(email=input("Garmin email: ").strip(), password=getpass.getpass("Garmin password: "),
                    prompt_mfa=lambda: getpass.getpass("Garmin MFA code: ").strip())
    path = token_directory(store)
    client.login(str(path))
    for file in path.iterdir():
        if file.is_file():
            file.chmod(0o600)
    store.set("profile", {"name": client.get_full_name() or "Your Garmin account"})
    print("Garmin connected. Session tokens saved locally; password is not persisted.")


def sync(store, osm=None):
    token_path = token_directory(store)
    if not (token_path / "garmin_tokens.json").exists():
        raise RuntimeError("Connect Garmin first: .venv/bin/python -m poc.garmin_sync login")
    client = Garmin()
    client.login(str(token_path))
    store.set("profile", {"name": client.get_full_name() or "Your Garmin account"})
    osm = osm or Overpass(allow_location_lookup=not store.get("location_lookup_pending", False))
    store.set("import_error", None)
    store.set("processing_error", None)
    full_history = not store.get("history_complete", False)
    offset, imported, skipped, errors = 0, 0, 0, 0
    while True:
        page = client.get_activities(offset, 100)
        if not isinstance(page, list):
            raise RuntimeError("Garmin returned an unexpected activity listing.")
        if not page:
            if full_history and not errors:
                store.set("history_complete", True)
            break
        new_in_page = False
        for activity in page:
            activity_id = int(activity["activityId"])
            if store.has_activity(activity_id):
                continue
            new_in_page = True
            key = activity.get("activityType", {}).get("typeKey", "")
            foot = any(word in key for word in ("running", "walking", "hiking"))
            store.set("sync", {"running": True, "stage": "Importing Garmin history", "scanned": offset,
                               "imported": imported, "skipped": skipped, "errors": errors})
            try:
                gpx = client.download_activity(activity_id, Garmin.ActivityDownloadFormat.GPX) if foot and activity.get("hasPolyline") is not False else b""
                store.add_activity(activity_id, activity.get("activityName", key), activity.get("startTimeGMT", ""), gpx)
                imported += bool(gpx)
                skipped += not bool(gpx)
            except Exception as error:
                errors += 1
                store.set("history_complete", False)
                # Never persist/log raw provider errors, which can contain credentials.
                store.set("import_error", {"activity_id": activity_id, "kind": type(error).__name__})
                if "TooManyRequests" in type(error).__name__ or "Authentication" in type(error).__name__:
                    raise
            time.sleep(0.5)
        offset += len(page)
        if len(page) < 100:
            if full_history and not errors:
                store.set("history_complete", True)
            break
        if not full_history and not new_in_page:
            break
    while not store.get("processing_error"):
        with store.db() as db:
            pending = db.execute("SELECT id FROM activities WHERE processed=0 ORDER BY date DESC").fetchall()
        if not pending:
            break
        for index, row in enumerate(pending):
            store.set("sync", {"running": True, "stage": "Discovering cities and matching streets",
                               "processed": index, "pending": len(pending), "errors": errors})
            try:
                store.process(row["id"], osm)
            except Exception as error:
                store.set("processing_error", {"activity_id": row["id"], "kind": type(error).__name__})
                errors += 1
                # Pause network-backed matching rather than repeatedly hammering a failed OSM service.
                break
    store.set("sync", {"running": False, "stage": "Idle" if not errors else "Paused with errors; retry sync",
                       "errors": errors, "finished_at": time.time()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["login", "sync", "reprocess"])
    args = parser.parse_args()
    store = Store()
    try:
        if args.command == "login":
            login(store)
        elif args.command == "sync":
            sync(store)
            print("Sync finished. Open the PoC map to inspect progress and errors.")
        else:
            with store.db() as db:
                db.execute("UPDATE activities SET processed=0")
                db.execute("DELETE FROM unresolved")
            print("Activities queued for reprocessing on the next sync.")
    except Exception as error:
        print(f"Garmin/OSM operation failed ({type(error).__name__}). Check connectivity, account login, and PoC status.")
        raise SystemExit(1) from None
