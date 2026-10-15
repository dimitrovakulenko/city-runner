"""Owner guards for the pinned SQL Lab form-based editor-state routes."""

import json
import re

from flask import abort, request


def guard_editor_state(user, account):
    path = request.path.rstrip('/')
    if not path.startswith(('/tabstateview', '/tableschemaview')):
        return False
    from superset import db
    from superset.models.core import Database
    from superset.models.sql_lab import Query, SavedQuery, TabState, TableSchema

    database = db.session.query(Database).filter_by(database_name=account).one()

    def own(model, identifier, field='id'):
        if field == 'client_id':
            if not isinstance(identifier, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,11}', identifier) is None:
                abort(400)
        else:
            if isinstance(identifier, bool) or re.fullmatch(r'[0-9]{1,10}', str(identifier)) is None:
                abort(400)
            identifier = int(identifier)
        value = db.session.query(model).filter_by(**{field: identifier}).one_or_none()
        if value is None or value.user_id != user.id:
            abort(404)
        return value

    def reference(fields, name, model):
        if fields.get(name) is not None:
            own(model, fields[name], 'client_id' if model is Query else 'id')

    def bound_database(fields, key):
        if key in fields and fields[key] not in (None, database.id):
            abort(403)

    try:
        if path == '/tabstateview' and request.method == 'POST':
            editor = json.loads(request.form['queryEditor'])
            bound_database(editor, 'dbId')
            reference(editor, 'remoteId', SavedQuery)
            return True
        tab = re.fullmatch(r'/tabstateview/(\d+)(?:/(activate|migrate_query|query/[^/]+))?', path)
        if tab:
            state = own(TabState, int(tab[1]))
            if state.database_id not in (None, database.id):
                abort(403)
            reference({'query': state.latest_query_id}, 'query', Query)
            action = tab[2]
            if action == 'migrate_query' and request.method == 'POST':
                own(Query, json.loads(request.form['queryId']), 'client_id')
            elif action and action.startswith('query/') and request.method == 'DELETE':
                own(Query, action[6:], 'client_id')
            elif action == 'activate' and request.method == 'POST':
                pass
            elif action is None and request.method in {'GET', 'HEAD', 'DELETE'}:
                pass
            elif action is None and request.method == 'PUT':
                fields = {key: json.loads(value) for key, value in request.form.items()}
                if fields.keys() - {'label', 'active', 'database_id', 'catalog', 'schema', 'sql',
                                    'query_limit', 'hide_left_bar', 'saved_query_id',
                                    'template_params', 'latest_query_id'}:
                    abort(403)
                bound_database(fields, 'database_id')
                reference(fields, 'saved_query_id', SavedQuery)
                reference(fields, 'latest_query_id', Query)
            else:
                abort(405)
            return True
        if path == '/tableschemaview' and request.method == 'POST':
            table = json.loads(request.form['table'])
            state = own(TabState, table['queryEditorId'])
            if state.database_id != database.id:
                abort(403)
            bound_database(table, 'dbId')
            return True
        schema = re.fullmatch(r'/tableschemaview/(\d+)(/expanded)?', path)
        if schema:
            table = db.session.query(TableSchema).filter_by(id=int(schema[1])).one_or_none()
            if table is None:
                abort(404)
            own(TabState, table.tab_state_id)
            if table.database_id != database.id:
                abort(403)
            if (schema[2] and request.method != 'POST') or (not schema[2] and request.method != 'DELETE'):
                abort(405)
            return True
    except (KeyError, TypeError, ValueError, AttributeError):
        abort(400)
    abort(405)
