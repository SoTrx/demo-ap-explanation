-- ERA5 daily readings for the City of Zurich cell (47.4, 8.5), 2005-2019, with the
-- column types of the dev server's ds_era5_land, so the meteo questions run as written.
-- Source: ap-explanation's scripts/meteo_examples/load_era5.py (Open-Meteo archive,
-- model era5_seamless, converted to ERA5 units: K, m, m/s). Run by psql at initdb,
-- which is what makes \copy work; files under data/ are not executed.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS provsql WITH SCHEMA public;

CREATE SCHEMA meteo;

CREATE TABLE meteo.meteo_elevation (latitude real, longitude real, elevation double precision);
CREATE TABLE meteo.meteo_tmin (latitude real, longitude real, time timestamp, tmin double precision);
CREATE TABLE meteo.meteo_tmax (latitude real, longitude real, time timestamp, tmax double precision);
CREATE TABLE meteo.meteo_tp (latitude real, longitude real, time timestamp, tp double precision);
CREATE TABLE meteo.meteo_windspeedmax (latitude real, longitude real, time timestamp, windspeedmax double precision);

\copy meteo.meteo_elevation FROM '/docker-entrypoint-initdb.d/data/meteo_elevation.csv' WITH CSV HEADER
\copy meteo.meteo_tmin FROM '/docker-entrypoint-initdb.d/data/meteo_tmin.csv' WITH CSV HEADER
\copy meteo.meteo_tmax FROM '/docker-entrypoint-initdb.d/data/meteo_tmax.csv' WITH CSV HEADER
\copy meteo.meteo_tp FROM '/docker-entrypoint-initdb.d/data/meteo_tp.csv' WITH CSV HEADER
\copy meteo.meteo_windspeedmax FROM '/docker-entrypoint-initdb.d/data/meteo_windspeedmax.csv' WITH CSV HEADER
