FROM ghcr.io/datagems-eosc/postgres-provsql:17-v1.12.0
COPY --chmod=755 01_meteo.sql /docker-entrypoint-initdb.d/01_meteo.sql
COPY --chmod=755 data/ /docker-entrypoint-initdb.d/data/
