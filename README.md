# City Runner

An experimental street-exploration app: import GPS activities, see visited OSM nodes and completed streets, and plan what to explore next.

Currently a local Python/FastAPI proof of concept with GPX import, approximate node coverage and a browser map. A production activity API, Android/iPhone fixture shell and infrastructure templates are merged. Production import/maps, real mobile accounts, approved provider integrations and the report → agent fix → verified preview workflow remain unfinished.

## Run the demo

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-poc.txt
.venv/bin/python -m poc.demo
POC_DATA_DIR=.poc-data/demo POC_PASSWORD=demo .venv/bin/python -m uvicorn poc.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> using `poc` / `demo`. The demo uses synthetic activities; private imports and credentials stay in ignored `.poc-data/`. This is a localhost experiment, not a hosted multi-user service.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Project documents

- [Build plan and risks](BUILD_PLAN.md)
- [Requirements](PROJECT.md)
- [Delivery backlog](BACKLOG.md)
- [Development milestones and subagent briefs](DEV_PLAN.md)
- [PoC setup, rules and limitations](POC.md)

The intended product is open source; a license has not yet been selected.
