FROM postgres:16.10@sha256:21f6013073bc6b92830a2129570e2f5ec42a6c734b5a985a41e83aa58f54c3c1
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-16-postgis-3=3.6.4+dfsg-2.pgdg13+1 && rm -rf /var/lib/apt/lists/*
