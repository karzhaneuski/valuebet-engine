"""Alembic environment wired to core.orm.Base and config.yaml."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import pool

from alembic import context
from core.config import load_config
from core.db import get_engine
from core.orm import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """DB URL precedence: -x db=... > DATABASE_URL env > config.yaml."""
    x_args = context.get_x_argument(as_dictionary=True)
    if "db" in x_args:
        return x_args["db"]
    import os
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return load_config().database.url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine(database_url())
    with engine.connect() as connection:
        # batch ALTERs on SQLite require disabled FK enforcement while
        # tables are rebuilt; a foreign_key_check afterwards proves the
        # data is still consistent. Only around real DDL runs — the
        # `alembic check` compare mode must not touch pragmas.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly ALTERs
            poolclass=pool.NullPool,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
