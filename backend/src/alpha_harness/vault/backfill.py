"""Filling the vault: every alpha you own, and its daily returns.

Metadata is cheap: a hundred alphas per listing request. Daily returns are one
``Retry-After`` request per alpha, so a thousand alphas is the better part of an hour —
hence a background task with visible progress, ordered by Sharpe so the alphas most
likely to be worth mixing arrive first.

None of this spends simulation quota. It is all reading results that already exist.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from ..brain.filters import AlphaQuery
from ..brain.schemas import Alpha
from .store import AlphaVault, checks_json
from .yields import is_promising, is_submittable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..brain.endpoints import BrainEndpoints
    from ..tasks import TaskRegistry

log = structlog.get_logger(__name__)

#: The alpha list's largest page: a bigger ``limit`` is served as 100 anyway.
PAGE = 100
#: The alpha list refuses offsets past 1,000. Beyond it the listing continues by
#: ``dateCreated`` instead.
MAX_OFFSET = 1_000

#: How many submission checks may be in flight at once. Each one is a ``Retry-After``
#: job on the platform's side.
CHECK_SLOTS = 2

#: Captures landing within this long of each other share one alpha-list request. A batch's
#: children land within a second or two of each other, and one list page carries the same
#: blocks as ten ``GET /alphas/{id}``.
CAPTURE_DEBOUNCE_SECONDS = 2.0
#: List pages read per capture batch before the rest fall back to a request each. Alphas
#: from batches not yet read back sit ahead of the landed ones on the newest page, so one
#: page is often not enough; newest-first paging only ever shifts rows later, never skips.
CAPTURE_PAGES = 3


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Backfill:
    """Brings the local vault up to date with the platform."""

    def __init__(
        self,
        vault: AlphaVault,
        endpoints: BrainEndpoints,
        tasks: TaskRegistry,
        *,
        on_checked: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.vault = vault
        self.endpoints = endpoints
        self.tasks = tasks
        self.on_checked = on_checked
        self._running: asyncio.Task[Any] | None = None
        self._check_slots = asyncio.Semaphore(CHECK_SLOTS)
        self._checking: set[str] = set()
        self._check_tasks: set[asyncio.Task[Any]] = set()
        #: Alphas whose daily PnL was asked for in the background, each once per run.
        self._returns_asked: set[str] = set()
        #: Alphas waiting for the next shared list read, each with what its caller awaits.
        self._landed: dict[str, asyncio.Future[None]] = {}
        self._drainer: asyncio.Task[None] | None = None
        #: How captures were read: from a shared list page, or one request each. If
        #: ``fetched`` rivals ``listed``, the list read is not saving anything.
        self.capture_counts = {"lists": 0, "listed": 0, "fetched": 0}
        #: How the last backfill ended. Kept past the task registry's short linger, so a
        #: sync that stopped early is still reported when someone looks.
        self.last: dict[str, Any] | None = None

    @property
    def busy(self) -> bool:
        return self._running is not None and not self._running.done()

    async def start(
        self,
        *,
        include_returns: bool = True,
        limit: int = 5000,
        since: datetime | None = None,
        resolve_checks: bool = True,
    ) -> str:
        """Begin a backfill in the background. Returns the task id.

        ``since`` makes it incremental: only alphas created after that moment are listed.
        ``resolve_checks=False`` stops after the listing, with no request per alpha.
        """
        if self.busy:
            raise RuntimeError("A backfill is already running.")
        task = await self.tasks.start("vault-backfill", "Importing your Alphas")
        self._running = asyncio.create_task(
            self._run(
                task,
                include_returns=include_returns,
                limit=limit,
                since=since,
                resolve_checks=resolve_checks,
            ),
            name="vault-backfill",
        )
        return task.id

    async def stop(self) -> None:
        # Awaited, not just cancelled: a check mid-``save_checks`` would otherwise still be
        # writing when shutdown closes the catalog under it.
        tasks = [*self._check_tasks, *(t for t in (self._drainer, self._running) if t)]
        for task in tasks:
            task.cancel()
        for future in self._landed.values():
            future.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running = None

    async def _run(
        self,
        task: Any,
        *,
        include_returns: bool,
        limit: int,
        since: datetime | None = None,
        resolve_checks: bool = True,
    ) -> None:
        imported = 0
        try:
            imported = await self._import_alphas(task, limit, since=since)
            if include_returns:
                await self._import_returns(task)
            resolved = await self.resolve_pending(task=task) if resolve_checks else 0
            await self.tasks.finish(task)
            self.last = {"state": "done", "imported": imported, "finishedAt": _now()}
            log.info("vault.backfill_done", alphas=imported, checked=resolved)
        except asyncio.CancelledError:
            await self.tasks.finish(task, state="cancelled")
            self.last = {"state": "cancelled", "imported": imported, "finishedAt": _now()}
            raise
        except Exception as exc:
            log.exception("vault.backfill_failed")
            await self.tasks.finish(task, state="failed", error=str(exc)[:300])
            self.last = {
                "state": "failed",
                "imported": imported,
                "error": str(exc)[:300],
                "finishedAt": _now(),
            }

    async def _import_alphas(self, task: Any, limit: int, *, since: datetime | None = None) -> int:
        """Page the pool into the vault, newest first."""
        await self.tasks.update(task, detail="Listing your Alphas", progress=0.0)
        offset = 0
        imported = 0
        before: datetime | None = None
        total: int | None = None

        while imported < limit:
            # hidden=None: an alpha you hid is still an alpha, and still has returns
            # worth correlating. Excluding them by default is a UI nicety, not a
            # property of the data.
            page = await self.endpoints.list_alphas(
                AlphaQuery(
                    limit=PAGE,
                    offset=offset,
                    hidden=None,
                    created_after=since,
                    created_before=before,
                )
            )
            results = page.get("results") or []
            if total is None:
                total = int(page.get("count") or 0)
            if not results:
                break

            alphas = [Alpha.model_validate(raw) for raw in results]
            await self.vault.save_alphas(alphas)
            # Reported on failure too, so "stopped after 1,050" is visible, not just "stopped".
            imported += len(alphas)
            await self.tasks.update(
                task,
                progress=min(1.0, imported / total) * 0.2 if total else None,
                detail=f"Imported {imported} of {total} Alphas",
                alphas=imported,
            )
            if len(results) < PAGE:
                break
            offset += PAGE
            if offset > MAX_OFFSET:
                # Newest first, so continue below the oldest alpha seen. One second past
                # it, because a multi-simulation creates several alphas in the same second
                # and ``dateCreated<`` is strict; the upsert makes the overlap harmless.
                oldest = alphas[-1].date_created
                if oldest is None:
                    break
                bound = oldest + timedelta(seconds=1)
                if before is not None and bound >= before:
                    log.warning("vault.import_window_stuck", imported=imported)
                    break
                before, offset = bound, 0

        return imported

    async def _import_returns(self, task: Any) -> int:
        """Fetch the daily series for every alpha that lacks one."""
        pending = await self.vault.without_returns()
        if not pending:
            await self.tasks.update(task, progress=1.0, detail="Daily returns already complete")
            return 0

        done = 0
        for alpha_id in pending:
            try:
                await self.fetch_returns(alpha_id)
            except Exception as exc:  # noqa: BLE001
                # One alpha's series failing must not abandon the rest; some alphas
                # genuinely have none.
                log.warning("vault.returns_failed", alpha_id=alpha_id, error=str(exc)[:160])
            done += 1
            await self.tasks.update(
                task,
                progress=0.2 + 0.8 * (done / len(pending)),
                detail=f"Daily returns: {done} of {len(pending)}",
                returns=done,
            )
        return done

    async def fetch_returns(self, alpha_id: str) -> int:
        """Fetch and store one alpha's daily series."""
        recordset = await self.endpoints.get_recordset(alpha_id, "daily-pnl")
        stored = await self.vault.save_pnl(alpha_id, recordset.rows())
        if recordset.records and not stored:
            # Days came back but none had a date and a PnL: the columns were renamed.
            columns = [p.name for p in recordset.schema_.properties]
            log.warning("vault.pnl_unreadable", alpha_id=alpha_id, columns=columns)
        return stored

    def schedule_returns(self, alpha_ids: list[str]) -> None:
        """Download these Alphas' daily PnL in the background, one after another.

        Each is asked for once while the process runs, so an Alpha without a series does not
        cost a request every time it is wanted.
        """
        fresh = [a for a in dict.fromkeys(alpha_ids) if a not in self._returns_asked]
        if not fresh:
            return
        self._returns_asked.update(fresh)
        task = asyncio.create_task(self._fetch_each(fresh), name="vault-returns")
        self._check_tasks.add(task)
        task.add_done_callback(self._check_tasks.discard)

    async def _fetch_each(self, alpha_ids: list[str]) -> None:
        for alpha_id in alpha_ids:
            try:
                await self.fetch_returns(alpha_id)
            except asyncio.CancelledError:
                raise
            # One alpha's series failing must not abandon the rest.
            except Exception as exc:  # noqa: BLE001
                log.warning("vault.returns_failed", alpha_id=alpha_id, error=str(exc)[:160])

    async def capture(self, alpha_id: str) -> None:
        """Record one alpha as soon as its simulation finishes. Returns once it is stored.

        Alphas that land together are read from one list page rather than a request each.
        Daily returns are *not* fetched here — they cost a request per alpha and are
        pulled on demand. Failure is logged and dropped, never a reason to fail the
        simulation that produced the alpha.
        """
        future = self._landed.get(alpha_id)
        if future is None:
            future = self._landed[alpha_id] = asyncio.get_running_loop().create_future()
            if self._drainer is None:
                self._drainer = asyncio.create_task(self._drain_landed(), name="vault-capture")
        await asyncio.shield(future)

    async def _drain_landed(self) -> None:
        try:
            while self._landed:
                await asyncio.sleep(CAPTURE_DEBOUNCE_SECONDS)
                batch, self._landed = self._landed, {}
                try:
                    await self._capture_batch(list(batch))
                # The drainer must outlive one bad batch, or later landings wait forever.
                except Exception:
                    log.warning("vault.capture_batch_failed", exc_info=True)
                finally:
                    for future in batch.values():
                        if not future.done():
                            future.set_result(None)
        finally:
            # No await between the loop's last check and here, so nothing lands unseen.
            self._drainer = None

    async def _capture_batch(self, alpha_ids: list[str]) -> None:
        """Store what the newest list page holds; read the rest one by one."""
        missing = set(alpha_ids)
        found: dict[str, Alpha] = {}
        complete: list[Alpha] = []
        try:
            for page_number in range(CAPTURE_PAGES):
                page = await self.endpoints.list_alphas(
                    AlphaQuery(limit=PAGE, offset=page_number * PAGE, hidden=None)
                )
                self.capture_counts["lists"] += 1
                results = page.get("results") or []
                for raw in results:
                    listed = Alpha.model_validate(raw)
                    if listed.id in missing:
                        missing.discard(listed.id)
                        found[listed.id] = listed
                if not missing or len(results) < PAGE:
                    break
            complete = list(found.values())
            if complete:
                await self.vault.save_alphas(complete)
        # Anything the page could not provide is read one by one below.
        except Exception:
            log.warning("vault.capture_list_failed", count=len(alpha_ids), exc_info=True)
            complete = []

        for alpha in complete:
            await self._captured(alpha)
        stored = {a.id for a in complete}
        fetch = [a for a in alpha_ids if a not in stored]
        self.capture_counts["listed"] += len(complete)
        self.capture_counts["fetched"] += len(fetch)
        log.info("vault.captured", listed=len(complete), fetched=len(fetch))
        for alpha_id in fetch:
            await self._capture_one(alpha_id)

    async def _capture_one(self, alpha_id: str) -> None:
        try:
            alpha = await self.endpoints.get_alpha(alpha_id)
            await self.vault.save_alpha(alpha)
        except Exception:
            log.warning("vault.capture_failed", alpha_id=alpha_id, exc_info=True)
            return
        await self._captured(alpha)

    async def _captured(self, alpha: Alpha) -> None:
        alpha_id = alpha.id
        # Tell open screens it is stored. The simulation's own "finished" broadcast lands
        # before this save, so a refetch on that alone would miss the new alpha.
        if self.on_checked is not None:
            # One failed notice must not cost the rest of its batch their capture.
            try:
                await self.on_checked({"alphaId": alpha_id, "stored": True})
            except Exception:
                log.warning("vault.capture_notify_failed", alpha_id=alpha_id, exc_info=True)

        # A finished simulation leaves the submission checks PENDING, so nothing is
        # submittable until the platform is asked to finish them.
        if is_promising(checks_json(alpha)):
            self.schedule_check(alpha_id)

    # -- submission checks -----------------------------------------------

    def schedule_check(self, alpha_id: str) -> None:
        """Finish this alpha's submission checks in the background.

        Deliberately not awaited. The tracker calls :meth:`capture` from inside its poll
        loop, and ``GET /alphas/{id}/check`` is a ``Retry-After`` job that can take a
        minute; blocking there would stall every other simulation's progress behind it.
        """
        if alpha_id in self._checking:
            return
        self._checking.add(alpha_id)
        task = asyncio.create_task(self._check(alpha_id), name=f"vault-check-{alpha_id}")
        self._check_tasks.add(task)
        task.add_done_callback(self._check_tasks.discard)

    async def _check(self, alpha_id: str) -> bool:
        """Ask the platform to resolve one alpha's checks, and store the answer."""
        try:
            async with self._check_slots:
                body = await self.endpoints.check_alpha(alpha_id)
            checks = ((body.get("is") or {}).get("checks")) or []
            if not checks:
                return False
            await self.vault.save_checks(alpha_id, checks)
            submittable = is_submittable(json.dumps(checks))
            log.info("vault.check_resolved", alpha_id=alpha_id, submittable=submittable)
            if self.on_checked is not None:
                await self.on_checked({"alphaId": alpha_id, "submittable": submittable})
            return submittable
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unresolved check is a gap in a convenience: the alpha is still stored,
            # still visible, and the next backfill will try again.
            log.warning("vault.check_failed", alpha_id=alpha_id, exc_info=True)
            return False
        finally:
            self._checking.discard(alpha_id)

    async def resolve_pending(self, *, limit: int = 200, task: Any = None) -> int:
        """Finish the checks on stored alphas the platform never resolved.

        Covers the two cases :meth:`capture` cannot: alphas that finished before this
        application started resolving checks, and alphas simulated on the BRAIN website
        rather than through here. Both should appear on the submit screen.
        """
        pending = await self.vault.awaiting_checks(limit=limit)
        if not pending:
            return 0

        done = 0
        for alpha_id in pending:
            await self._check(alpha_id)
            done += 1
            if task is not None:
                await self.tasks.update(task, detail=f"Submission checks: {done} of {len(pending)}")
        log.info("vault.checks_resolved", checked=done)
        return done
