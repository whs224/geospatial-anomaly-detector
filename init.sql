-- Runs once when the Postgres volume is first created. Schema objects live
-- in migrations.sql, which the ingestor and detector apply idempotently at
-- startup, so fresh and long-running databases converge on the same schema.

CREATE EXTENSION IF NOT EXISTS postgis;
