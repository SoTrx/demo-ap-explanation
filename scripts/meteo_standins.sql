-- Stand-ins for the tables the question set reads but the demo's seed (01_meteo.sql) lacks,
-- derived from the seed's readings so that every question runs as written. Run after
-- 01_meteo.sql, and after scripts/apx_service_check.py's copy of the readings to other cells
-- when there is one.
SET search_path TO meteo, public, provsql;

CREATE TABLE meteo_tmean AS
  SELECT n.latitude, n.longitude, n.time, (n.tmin + x.tmax) / 2 AS tmean
  FROM meteo_tmin n JOIN meteo_tmax x USING (latitude, longitude, time);
CREATE TABLE meteo_sdmax AS
  SELECT latitude, longitude, time,
         CASE WHEN EXTRACT(MONTH FROM time) IN (11, 12, 1, 2, 3)
              THEN GREATEST(0, (275.15 - tmin) * 0.01) ELSE 0 END AS sdmax
  FROM meteo_tmin;
CREATE TABLE meteo_ssrd AS
  SELECT latitude, longitude, time,
         1.2e7 * (1 - 0.7 * cos(2 * pi() * (EXTRACT(DOY FROM time) + 10) / 365.25))
           + 1e5 * (EXTRACT(DAY FROM time)::int % 7) AS ssrd
  FROM meteo_tmin;
CREATE TABLE landcover_upscaled (latitude real, longitude real, ranks int[]);
INSERT INTO landcover_upscaled VALUES (47.4, 8.5, ARRAY[[111, 40], [112, 30], [211, 20], [311, 10]]);
CREATE TABLE landcover_type (level1_code int, level1_label text, level2_code int, level2_label text,
                             level3_code int, level3_label text);
INSERT INTO landcover_type VALUES
  (1, 'Artificial surfaces', 11, 'Urban fabric', 111, 'Continuous urban fabric'),
  (1, 'Artificial surfaces', 11, 'Urban fabric', 112, 'Discontinuous urban fabric'),
  (2, 'Agricultural areas', 21, 'Arable land', 211, 'Non-irrigated arable land'),
  (3, 'Forest and semi natural areas', 31, 'Forests', 311, 'Broad-leaved forest');
