"""Download the BRAIN data catalog into DuckDB.

Quantitative research starts from data, so the catalog is the foundation everything else
reads: templates pick fields from it, the LLM is given its category tree, and the Data
tab is a direct view of it.

Scope is one ``(instrumentType, region, delay, universe)`` tuple per run, because that is
exactly how the platform scopes ``/data-fields`` — a field that exists in USA/delay-1
may simply not exist in EUR/delay-0. Storing one row per field *per tuple* is what makes
"which fields are in both delays" a single query later.

**Fields arrive in one request.** ``GET /data-fields`` at ``version=3.0`` with all four
scope parameters (``docs/wqb-api/endpoints/data.md``) returns the whole scope in one
response: 76,519 fields for USA/delay-1/TOP3000, verified live 2026-09-14 (probe P6).

Datasets are paged; categories are one request and do not depend on the scope (P7).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update

from ..brain.errors import BrainError
from ..brain.schemas import DataCategory, DataField, DataSet
from ..db.models import SyncRun, SyncStatus, utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from ..brain.endpoints import BrainEndpoints
    from ..db.duck import Catalog
    from ..db.sqlite import Database

log = structlog.get_logger(__name__)

#: Scopes whose fields are held in memory and downloading at once in a full sync.
ALL_CONCURRENCY = 4

#: Tries per market for each stage of a full sync. The client already retries throttling
#: and server errors per request; this also covers an empty answer or a failed write.
SCOPE_ATTEMPTS = 3

#: Progress steps per market state in a full sync: its fields arrive (1), its details arrive (2).
#: A market that failed, or kept only its fields, is finished.
_STEPS = {"waiting": 0, "fetching": 0, "details": 1, "fields": 2, "done": 2, "failed": 2}

ALL_LABEL = "All BRAIN datasets"

ProgressHook = Callable[[dict[str, Any]], Awaitable[None] | None]


class SyncCancelled(RuntimeError):
    """The user stopped a running sync."""


@dataclass(frozen=True, slots=True)
class SyncTarget:
    """The scope of one download."""

    instrument_type: str
    region: str
    delay: int
    universe: str

    @property
    def params(self) -> dict[str, Any]:
        return {
            "instrumentType": self.instrument_type,
            "region": self.region,
            "delay": self.delay,
            "universe": self.universe,
        }

    @property
    def label(self) -> str:
        return f"{self.instrument_type}/{self.region}/D{self.delay}/{self.universe}"


#: The run row that stands for a whole-catalog sync rather than one scope.
ALL_TARGET = SyncTarget("EQUITY", "*", -1, "*")


class CatalogSync:
    """Runs and reports catalog downloads."""

    def __init__(
        self,
        db: Database,
        catalog: Catalog,
        endpoints: BrainEndpoints,
        *,
        on_progress: ProgressHook | None = None,
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.endpoints = endpoints
        self._on_progress = on_progress
        self._cancels: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._targets: dict[int, SyncTarget] = {}
        #: Live stage and scope counts of a running full sync, by run id.
        self._all: dict[int, dict[str, Any]] = {}

    # -- public API ------------------------------------------------------

    async def start_all(self, targets: list[SyncTarget]) -> dict[str, Any]:
        """Download every scope in the background and return its progress immediately.

        One run row stands for the whole download, so there is one progress feed and one
        cancel. Every market runs its own pipeline at once: its fields first (with dataset
        and category rows derived from them, so it is browsable immediately), then BRAIN's
        full dataset and category records. A market that fails is noted and skipped rather
        than ending the sync.
        """
        for run_id in list(self._all):
            if run_id in self._tasks:
                live = await self.get_run(run_id)
                if live is not None:
                    return self._payload(live)

        run = await self._open_run(ALL_TARGET)
        cancel = asyncio.Event()
        self._cancels[run.id] = cancel
        self._targets[run.id] = ALL_TARGET
        self._all[run.id] = {
            "failed": [],
            # Per market, for the sync matrix: waiting, fetching, fields, details, done, failed.
            "markets": {
                t.label: {
                    "region": t.region,
                    "delay": t.delay,
                    "universe": t.universe,
                    "state": "waiting",
                    "fields": None,
                }
                for t in targets
            },
        }

        task = asyncio.create_task(
            self._guarded(run.id, ALL_LABEL, self._crawl_all(run.id, targets, cancel)),
            name=f"sync-all-{run.id}",
        )
        self._tasks[run.id] = task

        def _done(_t: asyncio.Task[None], rid: int = run.id) -> None:
            self._tasks.pop(rid, None)
            self._targets.pop(rid, None)
            self._all.pop(rid, None)

        task.add_done_callback(_done)
        return self._payload(run)

    async def reset_interrupted(self) -> int:
        """Mark runs left RUNNING by a crash as FAILED, so they stop reading as in progress.

        Called at startup, before anything can be running in this process.
        """
        async with self.db.session() as session:
            result = await session.execute(
                update(SyncRun)
                .where(SyncRun.status == SyncStatus.RUNNING)
                .values(
                    status=SyncStatus.FAILED,
                    error="The backend stopped during this sync.",
                    finished_at=utcnow(),
                )
            )
            return int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]  # DML returns a CursorResult; stubs say Result

    async def cancel(self, run_id: int) -> bool:
        event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    async def get_run(self, run_id: int) -> SyncRun | None:
        async with self.db.session() as session:
            return await session.get(SyncRun, run_id)

    async def runs(self, limit: int = 50) -> list[SyncRun]:
        async with self.db.session() as session:
            result = await session.execute(
                select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def shutdown(self) -> None:
        for event in self._cancels.values():
            event.set()
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- the download ----------------------------------------------------

    async def _guarded(self, run_id: int, label: str, work: Awaitable[None]) -> None:
        try:
            await work
        except SyncCancelled:
            await self._finish(run_id, SyncStatus.CANCELLED)
            log.info("sync.cancelled", run_id=run_id, target=label)
        except asyncio.CancelledError:
            await self._finish(run_id, SyncStatus.CANCELLED, error="Backend shut down")
            raise
        except BrainError as exc:
            await self._finish(run_id, SyncStatus.FAILED, error=exc.message)
            log.warning("sync.failed", run_id=run_id, error=exc.message)
        except Exception as exc:
            await self._finish(run_id, SyncStatus.FAILED, error=str(exc))
            log.exception("sync.crashed", run_id=run_id)
        finally:
            self._cancels.pop(run_id, None)

    async def _crawl_all(
        self, run_id: int, targets: list[SyncTarget], cancel: asyncio.Event
    ) -> None:
        progress = self._all[run_id]
        totals = {"fields": 0, "datasets": 0, "categories": 0}
        synced: list[SyncTarget] = []
        # Bounds how many markets hold a full field payload in memory at once.
        gate = asyncio.Semaphore(ALL_CONCURRENCY)
        # The taxonomy is the same for every scope, so it is read once. Locked because up to
        # ``ALL_CONCURRENCY`` markets reach this together and each would fetch its own copy.
        # Held only across the fetch, so a failure leaves it unset for the next market's
        # retry rather than failing the run.
        taxonomy: list[DataCategory] | None = None
        taxonomy_lock = asyncio.Lock()

        async def report() -> None:
            await self._update(
                run_id,
                fields_synced=totals["fields"],
                datasets_synced=totals["datasets"],
                categories_synced=totals["categories"],
                error=_failures(progress),
            )
            await self._emit(run_id)

        async def pipeline(target: SyncTarget) -> None:
            market = progress["markets"][target.label]
            async with gate:
                _check(cancel)
                market["state"] = "fetching"
                await self._emit(run_id)

                async def store() -> tuple[int, int, int]:
                    started = time.perf_counter()
                    raw = await self.endpoints.list_data_fields_all(**target.params)
                    fetched = time.perf_counter()
                    _check(cancel)
                    if not raw:
                        raise RuntimeError("BRAIN returned no data fields")
                    now = utcnow()
                    rows = await asyncio.to_thread(_field_rows, raw, target, now)
                    datasets, categories = await asyncio.to_thread(_derived_rows, raw, target, now)
                    prepared = time.perf_counter()
                    scope = [target.instrument_type, target.region, target.delay, target.universe]
                    await self.catalog.replace_fields(scope, rows)
                    # Placeholders only fill gaps: a re-sync must not blank the Value Score,
                    # Coverage and counts that an earlier details step stored.
                    await self.catalog.upsert_datasets(datasets, overwrite=False)
                    await self.catalog.upsert_categories(categories, overwrite=False)
                    # Where a full sync's time goes: BRAIN's response, row building, or the
                    # single-writer DuckDB lock (which also waits on the other scopes' writes).
                    log.info(
                        "sync_all.scope_timing",
                        target=target.label,
                        fields=len(rows),
                        fetch_s=round(fetched - started, 2),
                        prepare_s=round(prepared - fetched, 2),
                        write_s=round(time.perf_counter() - prepared, 2),
                    )
                    return len(rows), len(datasets), len(categories)

                try:
                    fields, datasets, categories = await _retry(store, cancel, target.label)
                except SyncCancelled:
                    raise
                # One market failing must not end the whole sync.
                except Exception as exc:  # noqa: BLE001
                    market["state"] = "failed"
                    progress["failed"].append(f"{target.label}: {_reason(exc)}")
                    log.warning("sync_all.fields_failed", target=target.label, error=_reason(exc))
                    await report()
                    return

            # Browsable now; BRAIN's full dataset and category records follow straight away.
            totals["fields"] += fields
            totals["datasets"] += datasets
            totals["categories"] += categories
            market.update(state="details", fields=fields)
            synced.append(target)
            await report()

            async def details() -> None:
                nonlocal taxonomy
                now = utcnow()
                async with taxonomy_lock:
                    if taxonomy is None:
                        taxonomy = await self.endpoints.list_data_categories()
                categories = taxonomy
                category_rows = [
                    row
                    for category in categories
                    for row in (
                        _category_row(category, None, target, now),
                        *(
                            _category_row(sub, category.id, target, now)
                            for sub in category.subcategories
                        ),
                    )
                ]
                await self.catalog.upsert_categories(category_rows)
                dataset_rows: list[tuple[Any, ...]] = []
                async for dataset in self.endpoints.iter_data_sets(**target.params):
                    _check(cancel)
                    dataset_rows.append(_dataset_row(dataset, target, now))
                await self.catalog.upsert_datasets(dataset_rows)

            try:
                # Inside the gate too: dataset paging is many requests per market, and every
                # market paging at once drew 429s that outlasted all retries.
                async with gate:
                    await _retry(details, cancel, target.label)
                market["state"] = "done"
            except SyncCancelled:
                raise
            # The market stays browsable on its derived rows.
            except Exception as exc:  # noqa: BLE001
                market["state"] = "fields"
                progress["failed"].append(f"{target.label} details: {_reason(exc)}")
                log.warning("sync_all.details_failed", target=target.label, error=_reason(exc))
            await report()

        # Every market's pipeline at once, bounded by the gate above.
        await self._update(run_id, phase="fields")
        await self._emit(run_id)
        results = await asyncio.gather(*(pipeline(t) for t in targets), return_exceptions=True)
        _check(cancel)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, SyncCancelled):
                raise result

        status = SyncStatus.COMPLETE if synced else SyncStatus.FAILED
        await self._finish(run_id, status, error=_failures(progress))
        log.info("sync_all.done", run_id=run_id, scopes=len(synced), failed=len(progress["failed"]))

    # -- run bookkeeping -------------------------------------------------

    async def _open_run(self, target: SyncTarget) -> SyncRun:
        async with self.db.session() as session:
            run = SyncRun(
                instrument_type=target.instrument_type,
                region=target.region,
                delay=target.delay,
                universe=target.universe,
                status=SyncStatus.RUNNING,
            )
            session.add(run)
            await session.flush()
            return run

    async def _update(self, run_id: int, **values: Any) -> None:
        async with self.db.session() as session:
            run = await session.get(SyncRun, run_id)
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)

    async def _finish(self, run_id: int, status: SyncStatus, *, error: str | None = None) -> None:
        await self._update(run_id, status=status, error=error, finished_at=utcnow())
        await self._emit(run_id)

    def _payload(self, run: SyncRun) -> dict[str, Any]:
        """A run's wire shape, plus live per-market states and progress while a full sync runs."""
        payload = serialise_run(run)
        progress = self._all.get(run.id)
        if progress:
            markets = list(progress["markets"].values())
            arrived = sum(m["state"] not in ("waiting", "fetching") for m in markets)
            payload |= {
                # "fields" while any market's fields are still on their way.
                "stage": "fields" if arrived < len(markets) else "details",
                "scopesDone": arrived,
                "scopesTotal": len(markets),
                "markets": markets,
                # One bar: each market's fields are half its share, its details the other half.
                "fraction": (
                    sum(_STEPS[m["state"]] for m in markets) / (2 * len(markets))
                    if markets
                    else 1.0
                ),
            }
        return payload

    async def _emit(self, run_id: int) -> None:
        if self._on_progress is None:
            return
        run = await self.get_run(run_id)
        if run is None:
            return
        try:
            result = self._on_progress(self._payload(run))
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("sync.progress_hook_failed", run_id=run_id)


def _check(cancel: asyncio.Event) -> None:
    if cancel.is_set():
        raise SyncCancelled


async def _retry(work: Callable[[], Awaitable[Any]], cancel: asyncio.Event, label: str) -> Any:
    """Run one market's step up to ``SCOPE_ATTEMPTS`` times, waiting 2 s then 4 s between tries."""
    for attempt in range(1, SCOPE_ATTEMPTS + 1):
        _check(cancel)
        try:
            return await work()
        except SyncCancelled:
            raise
        except Exception as exc:
            if attempt == SCOPE_ATTEMPTS:
                raise
            log.warning("sync_all.retry", target=label, attempt=attempt, error=_reason(exc))
            await asyncio.sleep(2 * attempt)
    return None


# -- row builders ---------------------------------------------------------
# Tuple order must match the *_COLUMNS constants in db.duck.


def _field_rows(
    raw: list[dict[str, Any]], target: SyncTarget, now: datetime
) -> list[tuple[Any, ...]]:
    return [_field_row(DataField.model_validate(item), target, now) for item in raw]


def _field_row(field: DataField, target: SyncTarget, now: datetime) -> tuple[Any, ...]:
    return (
        field.id,
        field.dataset.id if field.dataset else None,
        field.category.id if field.category else None,
        field.category.name if field.category else None,
        field.subcategory.id if field.subcategory else None,
        field.subcategory.name if field.subcategory else None,
        field.description,
        field.type,
        field.coverage,
        field.user_count,
        field.alpha_count,
        field.pyramid_multiplier,
        json.dumps(field.themes) if field.themes else None,
        target.instrument_type,
        target.region,
        target.delay,
        target.universe,
        now,
    )


def _dataset_row(dataset: DataSet, target: SyncTarget, now: datetime) -> tuple[Any, ...]:
    return (
        dataset.id,
        dataset.name,
        dataset.description,
        dataset.category.id if dataset.category else None,
        dataset.category.name if dataset.category else None,
        dataset.subcategory.id if dataset.subcategory else None,
        dataset.subcategory.name if dataset.subcategory else None,
        dataset.coverage,
        dataset.value_score,
        dataset.user_count,
        dataset.alpha_count,
        dataset.field_count,
        dataset.pyramid_multiplier,
        json.dumps(dataset.themes) if dataset.themes else None,
        target.instrument_type,
        target.region,
        target.delay,
        target.universe,
        now,
    )


def _category_row(
    category: DataCategory, parent_id: str | None, target: SyncTarget, now: datetime
) -> tuple[Any, ...]:
    return (
        category.id,
        category.name,
        parent_id,
        category.dataset_count,
        category.field_count,
        category.value_score,
        target.instrument_type,
        target.region,
        target.delay,
        target.universe,
        now,
    )


def _derived_rows(
    raw: list[dict[str, Any]], target: SyncTarget, now: datetime
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Dataset and category rows built from the fields alone.

    Every field names its dataset, category and subcategory, so a scope is browsable before
    its details arrive. Read from the raw dicts rather than re-validating ~85k models.
    Stage 2 of a full sync overwrites these with BRAIN's full records.
    """
    datasets: dict[str, dict[str, Any]] = {}
    categories: dict[str, dict[str, Any]] = {}
    for item in raw:
        dataset = item.get("dataset") or {}
        category = item.get("category") or {}
        subcategory = item.get("subcategory") or {}
        dataset_id = dataset.get("id")
        if dataset_id:
            entry = datasets.setdefault(
                dataset_id,
                {"name": dataset.get("name"), "category": category, "sub": subcategory, "n": 0},
            )
            entry["n"] += 1
        for node, parent_id in ((category, None), (subcategory, category.get("id"))):
            node_id = node.get("id")
            if not node_id:
                continue
            c = categories.setdefault(
                node_id, {"name": node.get("name"), "parent": parent_id, "ds": set(), "n": 0}
            )
            c["n"] += 1
            if dataset_id:
                c["ds"].add(dataset_id)

    scope = (target.instrument_type, target.region, target.delay, target.universe)
    dataset_rows = [
        (
            dataset_id,
            e["name"],
            None,
            e["category"].get("id"),
            e["category"].get("name"),
            e["sub"].get("id"),
            e["sub"].get("name"),
            None,
            None,
            None,
            None,
            e["n"],
            None,
            None,
            *scope,
            now,
        )
        for dataset_id, e in datasets.items()
    ]
    category_rows = [
        (node_id, c["name"], c["parent"], len(c["ds"]), c["n"], None, *scope, now)
        for node_id, c in categories.items()
    ]
    return dataset_rows, category_rows


def _reason(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _failures(progress: dict[str, Any]) -> str | None:
    """Failed scopes as one line for the run's ``error``; None while nothing has failed."""
    failed: list[str] = progress["failed"]
    if not failed:
        return None
    shown = "; ".join(failed[:5])
    more = f"; and {len(failed) - 5} more" if len(failed) > 5 else ""
    return f"{len(failed)} failed: {shown}{more}"


def serialise_run(run: SyncRun) -> dict[str, Any]:
    """Wire shape for the API and the live progress feed."""
    expected = run.fields_expected or 0
    fraction = (run.fields_synced / expected) if expected else None
    everything = run.region == ALL_TARGET.region
    return {
        "id": run.id,
        "instrumentType": run.instrument_type,
        "region": run.region,
        "delay": run.delay,
        "universe": run.universe,
        "all": everything,
        "label": ALL_LABEL
        if everything
        else f"{run.instrument_type}/{run.region}/D{run.delay}/{run.universe}",
        "status": run.status,
        "phase": run.phase,
        "cursorOffset": run.cursor_offset,
        "cursorDataset": run.cursor_dataset,
        "categoriesSynced": run.categories_synced,
        "datasetsSynced": run.datasets_synced,
        "fieldsSynced": run.fields_synced,
        "fieldsExpected": run.fields_expected,
        "fraction": fraction,
        "truncatedDatasets": list(run.truncated_datasets or []),
        "error": run.error,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
    }
