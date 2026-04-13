"""Alembic env. Runs schema.sql as the single source of truth for revisions."""
from __future__ import annotations

from alembic import context

from shared.db.connection import get_engine, init_schema


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            init_schema()


def run_migrations_offline() -> None:
    init_schema()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
