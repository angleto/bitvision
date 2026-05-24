-- Initial database bootstrap. Runs once when the pgvector/postgres
-- container starts with an empty data volume.
--
-- Schema objects themselves are created by Alembic migrations in the
-- backend service; this file only wires up database-level extensions
-- and roles that must exist before migrations run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Case-insensitive text for emails, etc.
CREATE EXTENSION IF NOT EXISTS citext;
