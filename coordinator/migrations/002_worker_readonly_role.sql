-- Rule 4: workers never write to Postgres. They read upstream records
-- through this role, which can only SELECT from the records table.
-- Roles belong to the whole server, so another database may have created
-- it already; that is fine.
DO $$
BEGIN
  CREATE ROLE worker_ro LOGIN PASSWORD 'worker_ro';
EXCEPTION
  WHEN duplicate_object OR unique_violation THEN NULL;
END
$$;

GRANT SELECT ON records TO worker_ro;
