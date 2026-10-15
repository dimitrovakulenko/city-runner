FROM apache/superset:6.0.0@sha256:6f6c9982bd64d81797bd4f33834f2ce4a4653c8ac8322f1e5f9df9b0332d092a
USER root
RUN uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.11
USER superset
