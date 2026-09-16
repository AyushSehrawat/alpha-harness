"""Async SQLAlchemy engine, session factory, and the additive schema migration.

WAL mode plus a busy timeout: the background simulation tracker writes while HTTP
handlers read, and SQLite's default rollback journal would make them block each other.

``create_all`` creates missing *tables*. It does not touch a table that already exists,
so a column added to a model after the database was created is simply never created —
and the failure arrives much later, as ``no such column`` from whichever query happens
to select it first. That is a bad way to find out.

:func:`migrate` closes that gap. On every startup the live schema is compared against
``Base.metadata`` and missing columns and indexes are added.

**Additive only, and it checks.** The policy in :mod:`.models` is that nothing is ever
dropped or renamed — losing a row in ``simulation_record`` means losing the ability to
cancel a running simulation. So a column that has *disappeared* from a model is not
quietly reconciled; it is reported, because that is a change this migrator is
deliberately not able to make safely and a real migration tool is then wanted.

Why not Alembic: it solves coordination between environments and people, and there is
one database on one machine here. What this application needs is that an existing
database never falls behind its models without saying so, which is about sixty lines.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Index, Table, UniqueConstraint, event, inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateColumn

from .models import Base

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _apply_pragmas(dbapi_connection: Any, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(url, future=True)
        event.listen(self._engine.sync_engine, "connect", _apply_pragmas)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def for_path(cls, path: Path) -> Database:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(f"sqlite+aiosqlite:///{path}")

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> dict[str, Any]:
        """Bring the schema up to the models. Additive only — never drops.

        Not just ``create_all``: that creates missing *tables* and leaves an existing
        table alone, so a column added to a model later never appears and the first
        query to select it fails with ``no such column``. See :func:`migrate`.
        """
        async with self._engine.begin() as conn:
            return await migrate(conn)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        async with self._engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return result.scalar() == 1

    async def dispose(self) -> None:
        await self._engine.dispose()


# --- migration -------------------------------------------------------------------

log = structlog.get_logger(__name__)


class MigrationError(RuntimeError):
    """A schema change this migrator will not make on its own."""


async def migrate(connection: AsyncConnection) -> dict[str, Any]:
    """Create what is missing and report what was done."""
    live_tables = set(await connection.run_sync(_table_names))
    await connection.run_sync(Base.metadata.create_all)

    added_columns: list[str] = []
    added_indexes: list[str] = []
    unknown_columns: list[str] = []
    missing_unique: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue  # create_all just made it, so it is current by construction.

        present = await _columns(connection, table.name)

        for column in table.columns:
            if column.name in present:
                continue
            ddl = _add_column_sql(table, column)
            await connection.execute(text(ddl))
            added_columns.append(f"{table.name}.{column.name}")

        expected = {c.name for c in table.columns}
        unknown_columns.extend(f"{table.name}.{name}" for name in sorted(present - expected))

        added_indexes.extend(
            [index.name or "" for index in table.indexes if await _create_index(connection, index)]
        )

        missing_unique.extend(await connection.run_sync(_missing_unique, table))

    if added_columns or added_indexes:
        log.info("db.migrated", columns=added_columns, indexes=added_indexes)
    if missing_unique:
        # Reported, not created: SQLite cannot add a constraint to an existing table, and a
        # unique index built over rows that already collide would stop startup instead.
        log.warning(
            "db.missing_unique_constraints",
            constraints=missing_unique,
            detail="Declared in the models but not enforced by the database. Needs a migration.",
        )
    if unknown_columns:
        # Not fatal: an extra column costs nothing at runtime and dropping it is exactly
        # the destructive act this migrator refuses to perform. But it means the model
        # and the database disagree, and that is worth saying out loud once per start.
        log.warning(
            "db.unknown_columns",
            columns=unknown_columns,
            detail=(
                "These exist in the database but not in the models. Nothing reads them. "
                "Removing them needs a real migration tool."
            ),
        )

    return {
        "columnsAdded": added_columns,
        "indexesAdded": added_indexes,
        "unknownColumns": unknown_columns,
        "missingUniqueConstraints": missing_unique,
    }


def _table_names(sync_connection: Any) -> list[str]:
    return list(inspect(sync_connection).get_table_names())


def _missing_unique(sync_connection: Any, table: Table) -> list[str]:
    """Declared unique constraints the live table does not enforce, matched by column set.

    Matched by columns, not name: SQLite names a constraint's index
    ``sqlite_autoindex_<table>_<n>``, so a name lookup would never find it.
    """
    inspector = inspect(sync_connection)
    live = [frozenset(u["column_names"]) for u in inspector.get_unique_constraints(table.name)]
    live += [
        frozenset(c for c in i["column_names"] if c is not None)
        for i in inspector.get_indexes(table.name)
        if i["unique"]
    ]
    return [
        f"{table.name}({', '.join(c.name for c in constraint.columns)})"
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and frozenset(c.name for c in constraint.columns) not in live
    ]


async def _columns(connection: AsyncConnection, table: str) -> set[str]:
    result = await connection.execute(text(f'PRAGMA table_info("{table}")'))
    return {row[1] for row in result.fetchall()}


async def _create_index(connection: AsyncConnection, index: Index) -> bool:
    """Create an index if it is missing. Returns whether it was created."""
    existing = await connection.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {"name": index.name},
    )
    if existing.first() is not None:
        return False
    await connection.run_sync(index.create)
    return True


def _add_column_sql(table: Table, column: Any) -> str:
    """``ALTER TABLE ... ADD COLUMN`` for one column.

    SQLite will not add a ``NOT NULL`` column to a table that already has rows without a
    constant default, and deriving one from a Python-side ``default`` has bitten before.
    So a new column is nullable or declares a ``server_default``, which SQLAlchemy renders
    into the DDL itself; anything else gets a real migration.
    """
    if not column.nullable and column.server_default is None:
        raise MigrationError(
            f"Cannot add {table.name}.{column.name}: it is NOT NULL with no server_default, "
            "so existing rows have no value to take. Make it nullable, give it a "
            "server_default, or write a real migration."
        )
    rendered = CreateColumn(column).compile(dialect=sqlite.dialect())
    return f'ALTER TABLE "{table.name}" ADD COLUMN {rendered}'


__all__ = ["AsyncSession", "Database"]
