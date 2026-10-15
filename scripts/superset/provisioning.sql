-- Synthetic D43 role/policy provisioning measurement; every change rolls back.
BEGIN;
CREATE TEMP TABLE provisioning_ms (ms double precision);
DO $$
DECLARE started timestamptz; role_name text;
BEGIN
    FOR i IN 1..25 LOOP
        role_name := 'pilot_bench_' || pg_backend_pid() || '_' || i;
        started := clock_timestamp();
        EXECUTE format('CREATE ROLE %I LOGIN CONNECTION LIMIT 2 PASSWORD %L', role_name, 'disposable-benchmark');
        EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', role_name);
        EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', role_name, '10s');
        EXECUTE format('ALTER ROLE %I SET temp_file_limit = %L', role_name, '16MB');
        EXECUTE format('GRANT CONNECT ON DATABASE analytics TO %I', role_name);
        EXECUTE format('GRANT USAGE ON SCHEMA analytics TO %I', role_name);
        EXECUTE format('GRANT SELECT ON analytics.activities TO %I', role_name);
        EXECUTE format('CREATE POLICY %I ON analytics.activities TO %I USING (account_id = %L)', role_name, role_name, 'pilot-a');
        INSERT INTO provisioning_ms VALUES (extract(epoch FROM clock_timestamp()-started)*1000);
    END LOOP;
END;
$$;
SELECT json_build_object('samples',count(*),'mean_ms',avg(ms),'p95_ms',percentile_cont(0.95) WITHIN GROUP (ORDER BY ms),'max_ms',max(ms)) FROM provisioning_ms;
ROLLBACK;
