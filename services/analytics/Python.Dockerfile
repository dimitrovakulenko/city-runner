FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
WORKDIR /repo
COPY backend/requirements.txt /dependencies/backend.txt
COPY scripts/superset/gateway-requirements.txt /dependencies/gateway.txt
RUN pip install --no-cache-dir -r /dependencies/backend.txt -r /dependencies/gateway.txt
COPY backend backend
COPY services/analytics services/analytics
RUN mkdir -p /var/lib/city-runner && chown 10001:10001 /var/lib/city-runner
ENV PYTHONPATH=/repo
USER 10001:10001
