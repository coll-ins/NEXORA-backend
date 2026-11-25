-- Provision the least-privilege application role nexora_app.
--
-- Run this ONCE per cluster as an administrator, after the schema exists
-- (i.e. after `flask db upgrade`). Grants are per database, so run it once per
-- database too:
--
--   psql -U postgres -d nexora_dev -v owner=nexora_user \
--       -f backend/scripts/nexora_app_role.sql
--
-- `owner` is the role that runs migrations. It is used so tables created later
-- by that user, not by postgres, receive the same privileges.
--
-- Why not in the migration: creating roles needs CREATEROLE, and a migration is
-- usually run by a plain database user that owns the schema but not the cluster.
-- Migration 0014 skips the role when it lacks that power and points here.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexora_app') THEN
        CREATE ROLE nexora_app NOLOGIN;
        RAISE NOTICE 'Created role nexora_app (no login; enable it with ALTER ROLE ... LOGIN PASSWORD ...).';
    ELSE
        RAISE NOTICE 'Role nexora_app already exists.';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO nexora_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nexora_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nexora_app;

-- History is append-only: no rewriting, no wiping, whatever the table set is.
DO $$
BEGIN
    IF to_regclass('public.transactions') IS NOT NULL THEN
        EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON transactions FROM nexora_app';
    END IF;
    IF to_regclass('public.audit_logs') IS NOT NULL THEN
        EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM nexora_app';
    END IF;
END
$$;

-- Tables and sequences created later by the migration user keep the baseline.
ALTER DEFAULT PRIVILEGES FOR ROLE :owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nexora_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO nexora_app;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nexora_app', current_database());
END
$$;

-- Verify what the role ended up with:
--   SELECT has_table_privilege('nexora_app', 'transactions', 'UPDATE');  -- false
--   SELECT has_table_privilege('nexora_app', 'transactions', 'INSERT');  -- true
