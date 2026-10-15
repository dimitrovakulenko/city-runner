"""Create two least-privilege authors and distinct database connections."""

from superset.app import create_app
from superset import db, security_manager


with create_app().app_context():
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database

    for suffix in ("a", "b"):
        username = f"pilot-{suffix}"
        role = security_manager.add_role(f"Pilot-{suffix}")
        # Gamma provides authoring; sql_lab adds query permissions. Database
        # access stays per-account. Do not give Alpha or all_database_access.
        permissions = {
            permission
            for name in ("Gamma", "sql_lab")
            for permission in security_manager.find_role(name).permissions
        }
        role.permissions = list(permissions)
        security_manager.add_permission_role(
            role, security_manager.add_permission_view_menu("can_write", "Dataset")
        )
        user = security_manager.find_user(username=username)
        if user is None:
            user = security_manager.add_user(
                username=username, first_name="Synthetic", last_name=suffix.upper(),
                email=f"{username}@invalid.example", role=role,
            )
        user.roles = [role]
        database = db.session.query(Database).filter_by(database_name=username).one_or_none()
        if database is None:
            database = Database(database_name=username)
            db.session.add(database)
        database.set_sqlalchemy_uri(
            f"postgresql+psycopg2://pilot_{suffix}:disposable-{suffix}@postgres/analytics"
        )
        database.expose_in_sqllab = True
        database.allow_run_async = False
        database.allow_ctas = False
        database.allow_cvas = False
        database.allow_dml = False
        database.allow_file_upload = False
        db.session.commit()
        security_manager.add_permission_role(
            role, security_manager.add_permission_view_menu("database_access", database.perm)
        )
        dataset = db.session.query(SqlaTable).filter_by(
            database_id=database.id, schema="analytics", table_name="activities",
        ).one_or_none()
        if dataset is None:
            dataset = SqlaTable(
                database=database, schema="analytics", table_name="activities", owners=[user],
            )
            db.session.add(dataset)
            db.session.commit()
            dataset.fetch_metadata()
        db.session.commit()
    print("Synthetic authors and owner-bound connections ready.")
