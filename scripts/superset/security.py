"""Narrow 6.1.0 adapter: private object ownership precedes dataset access."""

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.security import SupersetSecurityManager
from superset.utils.core import get_user_id


class PrivateQuerySecurityManager(SupersetSecurityManager):
    def raise_for_access(
        self, dashboard=None, chart=None, database=None, datasource=None,
        query=None, query_context=None, table=None, viz=None, sql=None,
        catalog=None, schema=None, template_params=None,
    ):
        for definition in (dashboard, chart):
            if definition is not None and get_user_id() not in {owner.id for owner in definition.owners}:
                raise SupersetSecurityException(SupersetError(
                    message="Cannot access another account's analytics object.",
                    error_type=SupersetErrorType.QUERY_SECURITY_ACCESS_ERROR,
                    level=ErrorLevel.ERROR,
                ))
        # Stock tableless query exports can pass the table-access check without
        # any accessible database. A query's creator is an independent boundary.
        if query is not None and hasattr(query, "user_id") and query.user_id != get_user_id():
            raise SupersetSecurityException(SupersetError(
                message="Cannot access another account's query.",
                error_type=SupersetErrorType.QUERY_SECURITY_ACCESS_ERROR,
                level=ErrorLevel.ERROR,
            ))
        return super().raise_for_access(
            dashboard=dashboard, chart=chart, database=database, datasource=datasource,
            query=query, query_context=query_context, table=table, viz=viz, sql=sql,
            catalog=catalog, schema=schema, template_params=template_params,
        )
