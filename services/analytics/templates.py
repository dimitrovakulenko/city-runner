"""Editable private starter copies; template updates never replace saved work."""

import json
import os
import uuid

STATUS = ("Data readiness", "SELECT * FROM analytics.dataset_status", [
    "dataset_id", "dataset_state", "coverage_status", "pending_sources", "failed_sources", "pending_imports"])
CITY = ("City progress", """SELECT city_id,city_name,rule,
    CASE WHEN bool_and(eligible_nodes IS NOT NULL) THEN count(*) ELSE NULL END AS eligible_streets,
    CASE WHEN bool_and(gps_complete IS NOT NULL) THEN count(*) FILTER (WHERE gps_complete) ELSE NULL END AS gps_complete_streets,
    count(*) FILTER (WHERE manual_complete) AS manual_complete_streets,
    CASE WHEN bool_and(effective_complete IS NOT NULL) THEN count(*) FILTER (WHERE effective_complete) ELSE NULL END AS effective_complete_streets
    FROM analytics.street_progress GROUP BY city_id,city_name,rule""", [
    "city_id", "city_name", "rule", "eligible_streets", "gps_complete_streets", "manual_complete_streets", "effective_complete_streets"])
TEMPLATES = {
    "activity": ("Activity", [
        ("Recorded totals", """SELECT count(*) AS activities,sum(distance_m) AS distance_m,
            count(*) FILTER (WHERE distance_m IS NULL) AS unknown_distance,
            sum(duration_s) AS duration_s,count(*) FILTER (WHERE duration_s IS NULL) AS unknown_duration,
            count(*) FILTER (WHERE recorded_date IS NULL) AS unknown_dates FROM analytics.activities""", [
            "activities", "distance_m", "unknown_distance", "duration_s", "unknown_duration", "unknown_dates"]),
        ("Activity types", """SELECT activity_type,count(*) AS activities,sum(distance_m) AS distance_m,
            count(*) FILTER (WHERE distance_m IS NULL) AS unknown_distance
            FROM analytics.activities GROUP BY activity_type""", ["activity_type", "activities", "distance_m", "unknown_distance"]),
        ("Monthly activity", """SELECT date_trunc('month',recorded_date)::date AS month,
            count(*) AS activities,sum(distance_m) AS distance_m,
            count(*) FILTER (WHERE distance_m IS NULL) AS unknown_distance
            FROM analytics.activities GROUP BY month""", ["month", "activities", "distance_m", "unknown_distance"]),
        ("Activities and explorer links", """SELECT activity_id,name,recorded_date,activity_type,distance_m,duration_s,pace_s_per_km,
            '<a href="__WEB__/?owner=__OWNER__&amp;activity=' || activity_id || '" target="_blank" rel="noopener noreferrer">Open activity</a>' AS open_activity
            FROM analytics.activities""", ["activity_id", "name", "recorded_date", "activity_type", "distance_m", "duration_s", "pace_s_per_km", "open_activity"]),
        STATUS,
    ]),
    "exploration": ("Exploration", [
        ("Retained history per activity", """SELECT *,
            '<a href="__WEB__/?owner=__OWNER__&amp;activity=' || activity_id || '" target="_blank" rel="noopener noreferrer">Open activity</a>' AS open_activity
            FROM analytics.exploration_history""", [
            "activity_id", "recorded_date", "rule", "supported_nodes", "new_nodes", "streets_advanced", "streets_completed", "history_status", "open_activity"]),
        ("New GPS nodes by day", """SELECT recorded_date,rule,
            CASE WHEN bool_and(history_status='ready' AND new_nodes IS NOT NULL)
              THEN sum(new_nodes) ELSE NULL END AS new_nodes,
            count(*) FILTER (WHERE history_status<>'ready' OR new_nodes IS NULL) AS unavailable_activities
            FROM analytics.exploration_history GROUP BY recorded_date,rule""", [
            "recorded_date", "rule", "new_nodes", "unavailable_activities"]),
        ("Retained GPS support growth by day", """WITH daily AS (
            SELECT recorded_date,rule,
              sum(CASE WHEN supported_nodes=0 THEN 0 ELSE new_nodes END) AS new_nodes,
              bool_and((history_status='ready' AND new_nodes IS NOT NULL) OR supported_nodes=0) AS ready
            FROM analytics.exploration_history GROUP BY recorded_date,rule), growth AS (
            SELECT recorded_date,rule,
              CASE WHEN bool_and(ready) OVER (PARTITION BY rule)
                THEN sum(new_nodes) OVER (PARTITION BY rule ORDER BY recorded_date ROWS UNBOUNDED PRECEDING)
                ELSE NULL END AS cumulative_retained_nodes,
              CASE WHEN bool_and(ready) OVER (PARTITION BY rule) THEN 'ready' ELSE 'unavailable' END AS history_status
            FROM daily)
            SELECT * FROM growth WHERE recorded_date IS NOT NULL""", ["recorded_date", "rule", "cumulative_retained_nodes", "history_status"]),
        CITY, STATUS,
    ]),
    "next-streets": ("Next streets", [
        CITY,
        ("Nearly complete streets (up to 200)", """SELECT street_id,city_id,city_name,street_name,rule,
            visited_nodes,eligible_nodes,eligible_nodes-visited_nodes AS remaining_nodes,
            visited_nodes::numeric / NULLIF(eligible_nodes,0) AS completion_ratio,
            '<a href="__WEB__/?owner=__OWNER__&amp;dataset=' || dataset_id || '&amp;city=' || city_id || '&amp;street=' || street_id || '" target="_blank" rel="noopener noreferrer">Open street</a>' AS open_street
            FROM analytics.street_progress WHERE NOT manual_complete AND NOT gps_complete
              AND visited_nodes::numeric / NULLIF(eligible_nodes,0)>=0.8""", [
            "street_id", "city_id", "city_name", "street_name", "rule", "visited_nodes", "eligible_nodes", "remaining_nodes", "completion_ratio", "open_street"]),
        STATUS,
    ]),
    "blank": ("My board", []),
}


def copy_template(user, database, key):
    from flask import abort
    from superset import db
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    if key not in TEMPLATES:
        abort(404)
    title, definitions = TEMPLATES[key]
    board = Dashboard(dashboard_title=title, owners=[user], published=False)
    db.session.add(board)
    db.session.flush()
    positions = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": [], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
    }
    charts = []
    for index, (name, sql, columns) in enumerate(definitions):
        sql = sql.replace("__WEB__", os.environ["WEB_ORIGIN"].rstrip("/").replace("'", "''")).replace("__OWNER__", user.username)
        dataset = SqlaTable(database=database, schema="analytics", table_name=f"starter_{uuid.uuid4().hex[:12]}",
            sql=sql, description=name + ". Personal editable copy; nullable values mean unavailable. See the City Runner dataset contract.", owners=[user])
        db.session.add(dataset)
        db.session.flush()
        dataset.fetch_metadata()
        filters = [{"col": "rule", "op": "==", "val": "normal"}] if "rule" in columns else []
        limit = 200 if "completion_ratio" in columns else 1000
        order = [["completion_ratio", False], ["street_id", True]] if limit == 200 else []
        params = {"viz_type": "table", "query_mode": "raw", "all_columns": columns, "row_limit": limit,
            "allow_render_html": True, "order_by_cols": [json.dumps(value) for value in order],
            "datasource": f"{dataset.id}__table", "dashboards": [board.id], "time_range": "No filter",
            "adhoc_filters": [{"expressionType": "SIMPLE", "subject": "rule", "operator": "==",
                "comparator": "normal", "clause": "WHERE", "sqlExpression": None}] if filters else []}
        context = {"datasource": {"id": dataset.id, "type": "table"}, "force": False,
            "queries": [{"columns": columns, "metrics": [], "row_limit": limit, "filters": filters,
                "orderby": order, "extras": {}, "time_range": "No filter"}], "result_format": "json", "result_type": "full"}
        chart = Slice(slice_name=name, viz_type="table", datasource_id=dataset.id, datasource_type="table",
                      params=json.dumps(params), query_context=json.dumps(context), owners=[user])
        db.session.add(chart)
        db.session.flush()
        charts.append(chart)
        row_id, chart_id = f"ROW-{index}", f"CHART-{index}"
        positions["GRID_ID"]["children"].append(row_id)
        positions[row_id] = {"id": row_id, "type": "ROW", "children": [chart_id], "parents": ["ROOT_ID", "GRID_ID"],
                             "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        positions[chart_id] = {"id": chart_id, "type": "CHART", "children": [], "parents": ["ROOT_ID", "GRID_ID", row_id],
            "meta": {"chartId": chart.id, "height": 50, "width": 12, "sliceName": name}}
    board.slices = charts
    board.position_json = json.dumps(positions)
    db.session.flush()
    return {"id": str(board.id), "title": title}


def initialize_starters(user, database):
    from superset import db

    extra = json.loads(database.extra or "{}")
    if extra.get("city_runner_starters_version"):
        return
    for key in ("activity", "exploration", "next-streets"):
        copy_template(user, database, key)
    database.extra = json.dumps({**extra, "city_runner_starters_version": 1})
    db.session.commit()
