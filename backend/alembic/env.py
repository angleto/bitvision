"""Alembic environment — async config pulling from bvphoenix.config."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from bvphoenix.config import get_settings
from bvphoenix.db import models
from bvphoenix.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # The ``alembic_version`` table is VARCHAR(32) by alembic's default,
    # but revision IDs like ``0007_opendata_pathology_constraints`` (35
    # chars) blow the limit and the upgrade aborts mid-way. We handle
    # both shapes of database:
    #
    # * Existing deploy: the table is there but narrow — ``ALTER`` widens
    #   it so it self-heals without an out-of-band migration.
    # * Fresh database (CI gate, new deploy): ``schema.sql`` does NOT
    #   define ``alembic_version`` (pg_dump excludes it), so alembic would
    #   create it at VARCHAR(32) on first use — too late for the ALTER's
    #   ``IF EXISTS`` to help. Pre-create it WIDE here so alembic adopts
    #   the existing wide table instead of making a narrow one.
    #
    # The explicit ``commit()`` is critical: SQLAlchemy 2.0's
    # ``engine.connect()`` returns a connection in "future" mode that
    # does NOT autocommit. Without this commit the DDL lives in an
    # outer transaction that gets implicitly rolled back when the
    # async wrapper closes the connection — alembic's own
    # ``begin_transaction`` opens a savepoint inside that outer
    # transaction and commits the savepoint, but the outer never
    # commits, so the ``UPDATE alembic_version`` plus every CREATE
    # TABLE issued by the migrations all vanish. Same fix below
    # after ``run_migrations`` covers that whole block too.
    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(255) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    )
    connection.exec_driver_sql(
        "ALTER TABLE IF EXISTS alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"
    )
    connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
