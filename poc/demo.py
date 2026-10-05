"""Seed a separate local demo using the real coverage calculation."""

from poc.core import DATA, Store


def seed():
    store = Store(DATA / "demo")
    if store.get("demo"):
        return store
    with store.db() as db:
        if db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]:
            raise RuntimeError("Demo directory already contains non-demo activities.")
    store.set("demo", True)
    for city_id, name, lon, lat in [(1, "Brussels · demo", 4.35, 50.85), (2, "Ghent · demo", 3.725, 51.054)]:
        ring = [(lon - .001, lat - .001), (lon + .009, lat - .001),
                (lon + .009, lat + .002), (lon - .001, lat + .002), (lon - .001, lat - .001)]
        relation = {"id": city_id, "tags": {"name": name}, "members": [{"type": "way", "role": "outer",
                    "geometry": [{"lon": x, "lat": y} for x, y in ring]}]}
        elements, tracks = [], []
        for street_index, state in enumerate(["Completed", "Partial", "Unvisited"]):
            nodes = [{"type": "node", "id": city_id * 100 + street_index * 10 + index,
                      "lon": lon + index * .0008, "lat": lat + street_index * .0006} for index in range(10)]
            elements.extend(nodes)
            elements.append({"type": "way", "id": city_id * 10 + street_index,
                             "tags": {"name": f"{state} demo street", "highway": "residential"},
                             "nodes": [node["id"] for node in nodes]})
            if street_index < 2:
                tracks.append(nodes[:10 if street_index == 0 else 5])
        store.add_city(relation, {"elements": elements})
        for index, nodes in enumerate(tracks):
            gpx = ('<gpx><trk><trkseg>' + ''.join(f'<trkpt lon="{n["lon"]}" lat="{n["lat"]}"/>' for n in nodes)
                   + '</trkseg></trk></gpx>').encode()
            store.add_activity(city_id * 10 + index, f"Synthetic {name} run {index + 1}", "2026-10-05", gpx)
    with store.db() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM activities")]
    for activity_id in ids:
        store.process(activity_id, None)
    store.set("profile", {"name": "Demo · synthetic GPS and streets"})
    store.set("history_complete", True)
    store.set("sync", {"running": False, "stage": "Demo calculations ready"})
    return store


if __name__ == "__main__":
    store = seed()
    print(f"Demo ready in {store.directory}: 2 cities, 6 streets, 4 activities.")
