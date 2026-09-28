-- Throwaway database for the storage tests. Separate from Airflow's own
-- metadata DB and, critically, from the Supabase instance that holds real
-- market data. The storage tests DROP and CREATE tables.
--
-- The suite refuses to run against any DSN whose database is not named
-- happening_bot_test, so pointing it at the wrong database fails loudly
-- rather than wiping production.
CREATE DATABASE happening_bot_test;
