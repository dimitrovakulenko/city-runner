"""Generate synthetic activities along original nodes of two public Gent streets."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import text
from backend.app.geography import import_osm_xml
from backend.tests.test_fit import fit_fixture, semicircles

CHECKSUM = '747e0b7675ee5e81b082c126ee3f8654f78e11f066840da1fac92d406085b951'
QUERY = '[out:xml][timeout:120];rel(897671)->.city;rel(897671);map_to_area->.area;(.city;way(r.city);way(area.area)["highway"]["name"];);out body;>;out skel qt;'

def prepare_gent(engine, path, fixtures):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != CHECKSUM:
        raise ValueError('Use the public Gent snapshot documented in docs/osm-import.md.')
    imported = import_osm_xml(engine, path, region='Gent', city_relation_ids=[897671],
        coverage_mode='complete', coverage_evidence='Public Overpass snapshot 2026-10-05T20:39:51Z; all named highway ways in Gent administrative area plus complete boundary and original nodes. Query: ' + QUERY)
    dataset = imported['dataset_id']
    with engine.connect() as db:
        streets = db.execute(text('''
            SELECT s.id,s.city_id,s.display_name,s.eligible_node_count,
              json_agg(ST_AsGeoJSON(w.geometry)::json ORDER BY w.osm_way_id) AS ways,
              ST_Distance(ST_Centroid(ST_Collect(w.geometry))::geography,
                ST_SetSRID(ST_MakePoint(3.724,51.054),4326)::geography) AS distance
            FROM streets s JOIN street_ways sw ON sw.street_id=s.id AND sw.dataset_id=s.dataset_id
              JOIN osm_ways w ON w.osm_way_id=sw.osm_way_id AND w.dataset_id=sw.dataset_id
            WHERE s.dataset_id=:dataset AND s.eligible_node_count BETWEEN 6 AND 40
            GROUP BY s.id HAVING sum(ST_NPoints(w.geometry)) <= 120
            ORDER BY distance,s.id LIMIT 2
        '''), {'dataset': dataset}).mappings().all()
        totals = dict(db.execute(text('''
            SELECT (SELECT count(*) FROM streets WHERE dataset_id=:dataset) AS streets,
              (SELECT count(*) FROM osm_nodes WHERE dataset_id=:dataset) AS nodes
        '''), {'dataset': dataset}).mappings().one())
    if len(streets) != 2:
        raise ValueError('Public Gent snapshot must supply two central streets for the walkthrough.')
    routes = []
    start = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
    for kind, street in zip(['gpx', 'fit'], streets):
        segments = [coordinates for way in street['ways'] for coordinates in way['coordinates']]
        name = f'Gent {kind.upper()} [synthetic]'
        filename = name + '.' + kind
        record = 0
        if kind == 'gpx':
            root = Element('gpx', version='1.1', creator='city-runner-synthetic-web-verification')
            track = SubElement(root, 'trk'); SubElement(track, 'name').text = name; SubElement(track, 'type').text = 'running'
            for segment in segments:
                track_segment = SubElement(track, 'trkseg')
                for lon, lat in segment:
                    point = SubElement(track_segment, 'trkpt', lon=str(lon), lat=str(lat))
                    SubElement(point, 'time').text = (start + timedelta(seconds=record * 5)).isoformat().replace('+00:00', 'Z')
                    record += 1
            content = tostring(root)
        else:
            epoch = datetime(1989, 12, 31, tzinfo=timezone.utc)
            first = int((start - epoch).total_seconds())
            records = []; events = []
            for segment in segments:
                if records:
                    events.extend([(len(records), first + len(records) * 5 - 1, 1), (len(records), first + len(records) * 5, 0)])
                for lon, lat in segment:
                    records.append((first + len(records) * 5, semicircles(lat), semicircles(lon)))
            content = fit_fixture(records=records, events=events)
        (fixtures / filename).write_bytes(content)
        routes.append({'kind': kind, 'filename': filename, 'name': name if kind == 'gpx' else 'Unknown activity',
            'street_id': str(street['id']), 'street_name': street['display_name'], 'city_id': str(street['city_id']),
            'eligible_nodes': street['eligible_node_count'], 'segments': segments})
    metadata = {'dataset_id': str(dataset), 'checksum': CHECKSUM, 'totals': totals, 'routes': routes}
    (fixtures / 'gent.json').write_text(json.dumps(metadata))
    print(json.dumps({'public_gent': totals, 'routes': [route['street_name'] for route in routes]}), flush=True)
    return metadata
