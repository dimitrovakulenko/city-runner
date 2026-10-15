FROM apache/superset:6.1.0@sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88
USER root
RUN uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.11
COPY patch_sql_lab.py /app/patch_sql_lab.py
RUN /app/.venv/bin/python /app/patch_sql_lab.py
USER superset
