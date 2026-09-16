"""DuckDB store for the data catalog.

A second engine because the catalog is ~85k fields *per* (instrumentType, region, delay,
universe) tuple and the Data Explorer's queries are columnar set operations across those
tuples. SQLite keeps the transactional state that must never be lost; DuckDB keeps the
bulk data that can always be re-synced.

The driver is synchronous and a connection is not safe for concurrent use, so writes are
serialised behind a lock, all off the event loop. Reads take their own cursor instead, which
MVCC lets run beside a write, so a long correlation read never stalls a sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = structlog.get_logger(__name__)

# One row per field per (instrumentType, region, delay, universe). The composite key is
# what makes availability checks and set operations a single indexed scan.
SCHEMA = """
CREATE TABLE IF NOT EXISTS data_field (
    field_id          VARCHAR NOT NULL,
    dataset_id        VARCHAR,
    category_id       VARCHAR,
    category_name     VARCHAR,
    subcategory_id    VARCHAR,
    subcategory_name  VARCHAR,
    description       VARCHAR,
    field_type        VARCHAR,
    coverage          DOUBLE,
    user_count        INTEGER,
    alpha_count       INTEGER,
    pyramid_multiplier DOUBLE,
    themes            VARCHAR,
    instrument_type   VARCHAR NOT NULL,
    region            VARCHAR NOT NULL,
    delay             INTEGER NOT NULL,
    universe          VARCHAR NOT NULL,
    synced_at         TIMESTAMP,
    PRIMARY KEY (field_id, instrument_type, region, delay, universe)
);

CREATE TABLE IF NOT EXISTS data_set (
    dataset_id        VARCHAR NOT NULL,
    name              VARCHAR,
    description       VARCHAR,
    category_id       VARCHAR,
    category_name     VARCHAR,
    subcategory_id    VARCHAR,
    subcategory_name  VARCHAR,
    coverage          DOUBLE,
    value_score       DOUBLE,
    user_count        INTEGER,
    alpha_count       INTEGER,
    field_count       INTEGER,
    pyramid_multiplier DOUBLE,
    themes            VARCHAR,
    instrument_type   VARCHAR NOT NULL,
    region            VARCHAR NOT NULL,
    delay             INTEGER NOT NULL,
    universe          VARCHAR NOT NULL,
    synced_at         TIMESTAMP,
    PRIMARY KEY (dataset_id, instrument_type, region, delay, universe)
);

CREATE TABLE IF NOT EXISTS data_category (
    category_id       VARCHAR NOT NULL,
    name              VARCHAR,
    parent_id         VARCHAR,
    dataset_count     INTEGER,
    field_count       INTEGER,
    value_score       DOUBLE,
    instrument_type   VARCHAR NOT NULL,
    region            VARCHAR NOT NULL,
    delay             INTEGER NOT NULL,
    universe          VARCHAR NOT NULL,
    synced_at         TIMESTAMP,
    PRIMARY KEY (category_id, instrument_type, region, delay, universe)
);

CREATE INDEX IF NOT EXISTS ix_field_tuple
    ON data_field (instrument_type, region, delay, universe);
CREATE INDEX IF NOT EXISTS ix_field_dataset ON data_field (dataset_id);
CREATE INDEX IF NOT EXISTS ix_field_category ON data_field (category_id);
CREATE INDEX IF NOT EXISTS ix_field_id ON data_field (field_id);
CREATE INDEX IF NOT EXISTS ix_set_tuple
    ON data_set (instrument_type, region, delay, universe);

-- Every alpha ever simulated, and its daily profit-and-loss series. Here rather than in
-- SQLite because pairwise correlation over ~2,500 daily values per alpha is a columnar
-- query, and because it is all re-fetchable from the platform.
CREATE TABLE IF NOT EXISTS alpha (
    alpha_id          VARCHAR PRIMARY KEY,
    expression        VARCHAR,
    sim_type          VARCHAR,
    instrument_type   VARCHAR,
    region            VARCHAR,
    delay             INTEGER,
    universe          VARCHAR,
    neutralization    VARCHAR,
    decay             INTEGER,
    truncation        DOUBLE,
    sharpe            DOUBLE,
    fitness           DOUBLE,
    turnover          DOUBLE,
    returns           DOUBLE,
    drawdown          DOUBLE,
    margin            DOUBLE,
    long_count        INTEGER,
    short_count       INTEGER,
    grade             VARCHAR,
    stage             VARCHAR,
    status            VARCHAR,
    operator_count    INTEGER,
    date_created      TIMESTAMP,
    checks            VARCHAR,
    fetched_at        TIMESTAMP
);

-- Computed from alpha_pnl on demand, so it survives re-imports of the alpha row.
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS k_ratio DOUBLE;
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS name VARCHAR;
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS date_submitted TIMESTAMP;
-- A simulation that holds its last years out as a test reports its train years apart.
-- Written only from Alphas that carry them, so a listing without them never blanks them.
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS train_sharpe DOUBLE;
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS train_fitness DOUBLE;
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS test_sharpe DOUBLE;
ALTER TABLE alpha ADD COLUMN IF NOT EXISTS test_fitness DOUBLE;

-- One row per alpha per trading day. ~2,500 rows per alpha.
CREATE TABLE IF NOT EXISTS alpha_pnl (
    alpha_id          VARCHAR NOT NULL,
    date              DATE NOT NULL,
    pnl               DOUBLE,
    PRIMARY KEY (alpha_id, date)
);

CREATE INDEX IF NOT EXISTS ix_alpha_scope
    ON alpha (instrument_type, region, delay, universe);
CREATE INDEX IF NOT EXISTS ix_alpha_sharpe ON alpha (sharpe);
CREATE INDEX IF NOT EXISTS ix_pnl_date ON alpha_pnl (date);
"""

FIELD_COLUMNS = (
    "field_id",
    "dataset_id",
    "category_id",
    "category_name",
    "subcategory_id",
    "subcategory_name",
    "description",
    "field_type",
    "coverage",
    "user_count",
    "alpha_count",
    "pyramid_multiplier",
    "themes",
    "instrument_type",
    "region",
    "delay",
    "universe",
    "synced_at",
)

DATASET_COLUMNS = (
    "dataset_id",
    "name",
    "description",
    "category_id",
    "category_name",
    "subcategory_id",
    "subcategory_name",
    "coverage",
    "value_score",
    "user_count",
    "alpha_count",
    "field_count",
    "pyramid_multiplier",
    "themes",
    "instrument_type",
    "region",
    "delay",
    "universe",
    "synced_at",
)

CATEGORY_COLUMNS = (
    "category_id",
    "name",
    "parent_id",
    "dataset_count",
    "field_count",
    "value_score",
    "instrument_type",
    "region",
    "delay",
    "universe",
    "synced_at",
)

ALPHA_COLUMNS = (
    "alpha_id",
    "expression",
    "sim_type",
    "instrument_type",
    "region",
    "delay",
    "universe",
    "neutralization",
    "decay",
    "truncation",
    "sharpe",
    "fitness",
    "turnover",
    "returns",
    "drawdown",
    "margin",
    "long_count",
    "short_count",
    "grade",
    "stage",
    "status",
    "operator_count",
    "date_created",
    "checks",
    "fetched_at",
    "name",
    "date_submitted",
)

#: Written only with an Alpha that has a ``train`` block (see ``AlphaVault.save_alphas``).
TRAIN_COLUMNS = ("train_sharpe", "train_fitness", "test_sharpe", "test_fitness")

PNL_COLUMNS = ("alpha_id", "date", "pnl")

_KEYS = {
    "data_field": ("field_id", "instrument_type", "region", "delay", "universe"),
    "data_set": ("dataset_id", "instrument_type", "region", "delay", "universe"),
    "data_category": ("category_id", "instrument_type", "region", "delay", "universe"),
    "alpha": ("alpha_id",),
    "alpha_pnl": ("alpha_id", "date"),
}

# Explicit Arrow types per column, matching the DuckDB schema above.
#
# Writes go through Arrow rather than SQL parameter binding, which was the bottleneck on
# large batches by three orders of magnitude. The types are stated rather than inferred
# because a column that happens to be all NULL in one page would otherwise infer as
# Arrow's null type and fail to insert into a typed column.
_STR = pa.string()
_F64 = pa.float64()
_I32 = pa.int32()
_TS = pa.timestamp("us", tz="UTC")
_DATE = pa.date32()

ARROW_TYPES: dict[str, dict[str, pa.DataType]] = {
    "data_field": {
        "field_id": _STR,
        "dataset_id": _STR,
        "category_id": _STR,
        "category_name": _STR,
        "subcategory_id": _STR,
        "subcategory_name": _STR,
        "description": _STR,
        "field_type": _STR,
        "coverage": _F64,
        "user_count": _I32,
        "alpha_count": _I32,
        "pyramid_multiplier": _F64,
        "themes": _STR,
        "instrument_type": _STR,
        "region": _STR,
        "delay": _I32,
        "universe": _STR,
        "synced_at": _TS,
    },
    "data_set": {
        "dataset_id": _STR,
        "name": _STR,
        "description": _STR,
        "category_id": _STR,
        "category_name": _STR,
        "subcategory_id": _STR,
        "subcategory_name": _STR,
        "coverage": _F64,
        "value_score": _F64,
        "user_count": _I32,
        "alpha_count": _I32,
        "field_count": _I32,
        "pyramid_multiplier": _F64,
        "themes": _STR,
        "instrument_type": _STR,
        "region": _STR,
        "delay": _I32,
        "universe": _STR,
        "synced_at": _TS,
    },
    "alpha": {
        "alpha_id": _STR,
        "expression": _STR,
        "sim_type": _STR,
        "instrument_type": _STR,
        "region": _STR,
        "delay": _I32,
        "universe": _STR,
        "neutralization": _STR,
        "decay": _I32,
        "truncation": _F64,
        "sharpe": _F64,
        "fitness": _F64,
        "turnover": _F64,
        "returns": _F64,
        "drawdown": _F64,
        "margin": _F64,
        "long_count": _I32,
        "short_count": _I32,
        "grade": _STR,
        "stage": _STR,
        "status": _STR,
        "operator_count": _I32,
        "date_created": _TS,
        "checks": _STR,
        "fetched_at": _TS,
        "k_ratio": _F64,
        "name": _STR,
        "date_submitted": _TS,
        "train_sharpe": _F64,
        "train_fitness": _F64,
        "test_sharpe": _F64,
        "test_fitness": _F64,
    },
    "alpha_pnl": {
        "alpha_id": _STR,
        "date": _DATE,
        "pnl": _F64,
    },
    "data_category": {
        "category_id": _STR,
        "name": _STR,
        "parent_id": _STR,
        "dataset_count": _I32,
        "field_count": _I32,
        "value_score": _F64,
        "instrument_type": _STR,
        "region": _STR,
        "delay": _I32,
        "universe": _STR,
        "synced_at": _TS,
    },
}


def _upsert_sql(
    table: str, columns: tuple[str, ...], source: str, *, overwrite: bool = True
) -> str:
    """``INSERT ... SELECT`` from a registered Arrow relation, with upsert semantics.

    ``overwrite=False`` inserts only keys not stored yet and leaves existing rows untouched.
    """
    key = _KEYS[table]
    column_list = ", ".join(columns)
    insert = (
        f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {source} "  # noqa: S608
        f"ON CONFLICT ({', '.join(key)}) "
    )
    if not overwrite:
        return insert + "DO NOTHING"
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in key)
    return insert + f"DO UPDATE SET {updates}"


def _to_arrow(table: str, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> pa.Table:
    """Transpose row tuples into a typed Arrow table."""
    types = ARROW_TYPES[table]
    transposed = list(zip(*rows, strict=True))
    return pa.table(
        {
            name: pa.array(values, type=types[name])
            for name, values in zip(columns, transposed, strict=True)
        }
    )


class CatalogLockedError(RuntimeError):
    """Another Alpha Harness backend already has the catalog open.

    DuckDB allows exactly one writer, so the fix is always the same: stop the other one.
    """

    def __init__(self, path: Path, detail: str) -> None:
        holder = ""
        match = re.search(r"PID (\d+)", detail)
        if match:
            holder = f" (process {match.group(1)})"
        super().__init__(
            f"Another Alpha Harness backend{holder} is already running and has "
            f"{path} open. DuckDB allows only one writer, so stop the other one and "
            "start again. If you believe nothing else is running, the previous process "
            "did not shut down cleanly — end it and retry."
        )
        self.path = path
        self.detail = detail


class Catalog:
    """Async facade over a DuckDB file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()
        #: Reads in flight. Futures rather than a count: a cancelled caller does not stop
        #: its thread, and ``close`` must wait for the thread, not the caller.
        self._reads: set[asyncio.Future[Any]] = set()
        self._closing = False

    async def open(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        await asyncio.to_thread(self._open_sync)
        log.info("catalog.opened", path=str(self.path))

    def _open_sync(self) -> None:
        try:
            # DuckDB's zone otherwise defaults to the machine's, shifting every stored
            # time by the local offset. Set here, not with SET, so read cursors get it too.
            self._conn = duckdb.connect(str(self.path), config={"TimeZone": "UTC"})
        except duckdb.IOException as exc:
            # DuckDB is single-writer, so a second backend is the likely cause. The raw
            # exception is a wall of text ending in a URL; say the useful thing instead.
            if "lock" not in str(exc).lower():
                raise
            raise CatalogLockedError(self.path, str(exc)) from exc
        self._conn.execute(SCHEMA)

    async def close(self) -> None:
        """Refuse new reads, then let in-flight writes and reads finish before closing."""
        self._closing = True
        async with self._lock:
            await asyncio.gather(*self._reads, return_exceptions=True)
            if self._conn is not None:
                await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    async def _locked[T](self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` on the one connection, holding the lock until the thread is done.

        A thread cannot be cancelled, so if the awaiting task is, the lock must still not
        be released while the thread holds the non-thread-safe connection.
        """
        async with self._lock:
            work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await work
                raise

    def _require(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("Catalog is not open; call await catalog.open() first")
        return self._conn

    # -- reads -----------------------------------------------------------

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a read query and return rows as dicts. Does not wait for writes."""
        if self._closing or self._conn is None:
            raise RuntimeError("Catalog is closed or shutting down")
        work = asyncio.ensure_future(asyncio.to_thread(self._query_sync, sql, params or []))
        self._reads.add(work)
        work.add_done_callback(self._reads.discard)
        return await asyncio.shield(work)

    def _query_sync(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        with self._require().cursor() as cur:
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description or []]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    async def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        rows = await self.query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    # -- writes ----------------------------------------------------------

    async def upsert(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: list[tuple[Any, ...]],
        *,
        overwrite: bool = True,
    ) -> int:
        """Insert or update a batch. Returns the number of rows written.

        Goes through Arrow — see :data:`ARROW_TYPES` for why. Re-syncing the same scope
        updates rows in place rather than duplicating them; ``overwrite=False`` only adds
        rows whose key is new.
        """
        if not rows:
            return 0
        await self._locked(self._upsert_sync, table, columns, rows, overwrite)
        return len(rows)

    def _upsert_sync(
        self, table: str, columns: tuple[str, ...], rows: list[tuple[Any, ...]], overwrite: bool
    ) -> None:
        conn = self._require()
        arrow_table = _to_arrow(table, columns, rows)
        source = "_incoming"
        conn.register(source, arrow_table)
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(_upsert_sql(table, columns, source, overwrite=overwrite))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.unregister(source)

    async def replace_fields(self, scope: list[Any], rows: list[tuple[Any, ...]]) -> int:
        """Make one scope's fields exactly ``rows``, in one transaction.

        Upsert first, then drop what the download no longer lists: a field BRAIN has
        removed would otherwise stay selectable and spend a simulation on an error.
        Delete-then-insert is not an option, because DuckDB checks the primary key
        eagerly inside a transaction.
        """
        if not rows:
            return 0
        await self._locked(self._replace_fields_sync, scope, rows)
        return len(rows)

    def _replace_fields_sync(self, scope: list[Any], rows: list[tuple[Any, ...]]) -> None:
        conn = self._require()
        source = "_incoming"
        conn.register(source, _to_arrow("data_field", FIELD_COLUMNS, rows))
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(_upsert_sql("data_field", FIELD_COLUMNS, source))
                conn.execute(
                    "DELETE FROM data_field WHERE instrument_type = ? AND region = ? "  # noqa: S608
                    "AND delay = ? AND universe = ? "
                    f"AND field_id NOT IN (SELECT field_id FROM {source})",
                    scope,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.unregister(source)

    async def upsert_datasets(self, rows: list[tuple[Any, ...]], *, overwrite: bool = True) -> int:
        return await self.upsert("data_set", DATASET_COLUMNS, rows, overwrite=overwrite)

    async def upsert_categories(
        self, rows: list[tuple[Any, ...]], *, overwrite: bool = True
    ) -> int:
        return await self.upsert("data_category", CATEGORY_COLUMNS, rows, overwrite=overwrite)

    async def upsert_alphas(self, rows: list[tuple[Any, ...]]) -> int:
        return await self.upsert("alpha", ALPHA_COLUMNS, rows)

    async def upsert_pnl(self, rows: list[tuple[Any, ...]]) -> int:
        return await self.upsert("alpha_pnl", PNL_COLUMNS, rows)
