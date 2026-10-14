-- Prepares a database seeded with 01_meteo.sql and scripts/meteo_standins.sql for
-- scripts/provsql_compare.py: what ap-explanation does before it runs a query: a provsql_ref
-- column on every table, add_provenance, one mapping per table and a union view per semiring.
SET search_path TO meteo, public, provsql;
SET provsql.active = 1;

DO $$
DECLARE t text; u text := '';
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'meteo' ORDER BY 1 LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN provsql_ref text DEFAULT %L || translate(gen_random_uuid()::text, ''-'', '''')', t, t || '@k');
    PERFORM add_provenance(t::regclass);
    PERFORM create_provenance_mapping(t || '_map', t, 'provsql_ref');
    u := u || CASE WHEN u = '' THEN '' ELSE ' UNION ALL ' END || format('SELECT value, provenance FROM %I', t || '_map');
  END LOOP;
  FOR t IN SELECT unnest(ARRAY['formula', 'why', 'how', 'which', 'boolexpr']) LOOP
    EXECUTE format('CREATE VIEW %I AS %s', t || '_mapping', u);
  END LOOP;
END $$;
ANALYZE;
