-- Disposable synthetic data only. Never run against an application database.
CREATE USER superset_meta PASSWORD 'disposable-metadata';
CREATE DATABASE superset_meta OWNER superset_meta;
CREATE DATABASE analytics;
CREATE USER pilot_sessions PASSWORD 'disposable-sessions';
CREATE DATABASE pilot_sessions OWNER pilot_sessions;
\connect pilot_sessions pilot_sessions
CREATE TABLE accounts (id varchar(255) PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE login_identities (
    provider varchar(16) NOT NULL, subject varchar(255) NOT NULL,
    account_id varchar(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (provider, subject)
);
CREATE TABLE sessions (
    token_digest char(64) PRIMARY KEY,
    account_id varchar(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL, revoked_at timestamptz
);
CREATE TABLE account_deletions (account_id varchar(255) PRIMARY KEY);
INSERT INTO accounts(id) VALUES ('pilot-a'), ('pilot-b');
INSERT INTO login_identities VALUES
    ('google','synthetic-pilot-a','pilot-a'), ('google','synthetic-pilot-b','pilot-b');
\connect analytics postgres
REVOKE ALL ON DATABASE analytics FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE ROLE pilot_a LOGIN PASSWORD 'disposable-a' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE pilot_b LOGIN PASSWORD 'disposable-b' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE analytics TO pilot_a, pilot_b;
CREATE SCHEMA analytics;
CREATE TABLE analytics.activities (
    account_id text NOT NULL,
    activity_id bigint NOT NULL,
    recorded_at timestamptz,
    activity_type text,
    distance_m double precision,
    duration_s double precision
);
INSERT INTO analytics.activities VALUES
    ('pilot-a', 1, '2026-10-01T09:00:00Z', 'run', 5000, 1800),
    ('pilot-a', 2, '2026-10-02T09:00:00Z', 'walk', NULL, NULL),
    ('pilot-b', 3, '2026-10-01T09:00:00Z', 'run', 9000, 3600);
ALTER TABLE analytics.activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.activities FORCE ROW LEVEL SECURITY;
CREATE POLICY account_a ON analytics.activities TO pilot_a USING (account_id = 'pilot-a');
CREATE POLICY account_b ON analytics.activities TO pilot_b USING (account_id = 'pilot-b');
GRANT USAGE ON SCHEMA analytics TO pilot_a, pilot_b;
GRANT SELECT ON analytics.activities TO pilot_a, pilot_b;
-- Synthetic-only deadline fixture. Demonstrates why the SQL denylist is not a firewall.
CREATE FUNCTION analytics.pilot_slow(seconds double precision)
RETURNS void LANGUAGE sql AS $$ SELECT pg_sleep(seconds) $$;
CREATE FUNCTION analytics.pilot_slow_value(seconds double precision)
RETURNS integer LANGUAGE sql AS $$ SELECT 1 FROM pg_sleep(seconds) $$;
CREATE SCHEMA excluded;
CREATE TABLE excluded.credentials (secret text);
INSERT INTO excluded.credentials VALUES ('synthetic-secret-must-never-be-returned');
ALTER ROLE pilot_a SET statement_timeout = '10s';
ALTER ROLE pilot_b SET statement_timeout = '10s';
ALTER ROLE pilot_a SET default_transaction_read_only = on;
ALTER ROLE pilot_b SET default_transaction_read_only = on;
-- Two admitted SQL requests plus one separately admitted cancellation connection.
ALTER ROLE pilot_a CONNECTION LIMIT 3;
ALTER ROLE pilot_b CONNECTION LIMIT 3;
ALTER ROLE pilot_a SET temp_file_limit = '16MB';
ALTER ROLE pilot_b SET temp_file_limit = '16MB';
CREATE ROLE pilot_cancel LOGIN PASSWORD 'disposable-cancel' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE analytics TO pilot_cancel;
CREATE SCHEMA control;
CREATE FUNCTION control.terminate_query(owner text, target integer, started timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE target_role text;
BEGIN
    target_role := CASE owner WHEN 'pilot-a' THEN 'pilot_a' WHEN 'pilot-b' THEN 'pilot_b' END;
    IF target_role IS NULL THEN RETURN false; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=target
        AND backend_start=started AND datname='analytics' AND usename=target_role
        AND backend_type='client backend') THEN RETURN true; END IF;
    PERFORM pg_catalog.pg_terminate_backend(target, 500);
    PERFORM pg_catalog.pg_stat_clear_snapshot();
    RETURN NOT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=target AND backend_start=started);
END;
$$;
REVOKE ALL ON FUNCTION control.terminate_query(text, integer, timestamptz) FROM PUBLIC;
GRANT USAGE ON SCHEMA control TO pilot_cancel;
GRANT EXECUTE ON FUNCTION control.terminate_query(text, integer, timestamptz) TO pilot_cancel;
