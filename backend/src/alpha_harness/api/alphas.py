"""The alpha pool on BRAIN: summary, the Alpha page, check, correlate, edit properties.

**There is no submit endpoint, deliberately.** Submitting an alpha is irreversible, so the
guard against doing it by accident is that no route, service or client wrapper for it
exists. Correlations are slow, rate-limited jobs, so their answers are kept in
``brain_cache`` until the user refreshes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, RootModel
from sqlalchemy import delete, func, select

from ..brain.errors import BrainError
from ..db.models import BrainCache, SimulationRecord, Study, Trial, TrialState, utcnow
from ..labs.fastexpr import ParseError, data_fields, operator_count, parse
from ..labs.params import TASK_SAMPLERS
from ..schemas import Out
from ..vault.yields import PLATFORM_ALPHA_URL
from .deps import State

router = APIRouter(prefix="/api/alphas", tags=["alphas"])

CorrelationKind = Literal["self", "prod", "power-pool"]
Refresh = Annotated[bool, Query(description="Ask BRAIN again instead of reading the cache.")]
CachedOnly = Annotated[
    bool, Query(description="Answer only from the cache; never spend BRAIN's budget.")
]


class BrainPayload(RootModel[dict[str, Any]]):
    """BRAIN's own JSON, passed through as the platform sent it."""


class AlphaStats(Out):
    pnl: float | None = None
    book_size: float | None = None
    long_count: int | None = None
    short_count: int | None = None
    turnover: float | None = None
    returns: float | None = None
    drawdown: float | None = None
    margin: float | None = None
    sharpe: float | None = None
    fitness: float | None = None


class AlphaYear(AlphaStats):
    year: str
    stage: str | None = None


class AlphaClassification(Out):
    id: str
    name: str


class AlphaInfo(Out):
    alpha_id: str
    type: str | None
    author: str | None
    code: str | None
    description: str | None
    operator_count: int | None
    name: str | None
    category: str | None
    color: str | None
    tags: list[str]
    stage: str | None
    status: str | None
    date_created: str | None
    date_submitted: str | None
    date_modified: str | None
    #: Every simulation setting BRAIN reports, as it named them.
    settings: dict[str, Any]
    classifications: list[AlphaClassification]
    in_sample: AlphaStats | None
    investability: AlphaStats | None
    #: BRAIN's checks as sent, extras included (pyramids, themes, competitions).
    checks: list[dict[str, Any]]
    brain_url: str
    #: Operators as Power Pool counts them (backfills excluded); null when the code is unreadable.
    power_pool_operators: int | None
    #: Distinct data fields, grouping fields excluded; null when the code is unreadable.
    data_fields: list[str] | None


class LineageSibling(Out):
    alpha_id: str
    expression: str | None
    value: float | None


class AlphaLineage(Out):
    task: str
    lab: str | None
    lab_name: str | None
    study_id: int | None
    study_name: str | None
    template_name: str | None
    #: What the lab chose for this Alpha: its dataset, template variables, generation.
    params: dict[str, Any]
    generation: int | None
    simulated_at: str | None
    #: The task's best other Alphas on what it searches for.
    siblings: list[LineageSibling]


class AlphaView(Out):
    alpha: AlphaInfo
    dates: list[str]
    #: Cumulative, one per trading day.
    pnl: list[float | None]
    investability_pnl: list[float | None]
    yearly: list[AlphaYear]
    lineage: AlphaLineage | None
    #: When the Alpha itself was last read from BRAIN; series are kept until refreshed.
    fetched_at: str
    #: Anything that could not be loaded, said plainly.
    problems: list[str]


class AlphaProperties(BaseModel):
    name: str | None = None
    category: str | None = None
    color: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None


# -- cache ---------------------------------------------------------------


async def _cached(
    state: State,
    key: str,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    refresh: bool,
) -> tuple[dict[str, Any], datetime]:
    """A cached BRAIN answer, fetched and stored when missing or when ``refresh``."""
    async with state.db.session() as session:
        row = await session.get(BrainCache, key)
        if row is not None and not refresh:
            return row.body, row.fetched_at
    body = await fetch()
    now = utcnow()
    async with state.db.session() as session:
        row = await session.get(BrainCache, key)
        if row is None:
            session.add(BrainCache(key=key, body=body, fetched_at=now))
        else:
            row.body, row.fetched_at = body, now
    return body, now


async def _forget(state: State, *keys: str) -> None:
    async with state.db.session() as session:
        await session.execute(delete(BrainCache).where(BrainCache.key.in_(keys)))


async def _kept(state: State, key: str) -> BrainPayload:
    async with state.db.session() as session:
        row = await session.get(BrainCache, key)
    if row is None:
        return BrainPayload({"cached": False})
    return BrainPayload(row.body | {"cached": True, "fetchedAt": _iso(row.fetched_at)})


def _iso(value: datetime) -> str:
    return value.isoformat()


# -- reading BRAIN's shapes ---------------------------------------------------


def _stats(raw: Any) -> AlphaStats | None:
    return AlphaStats.model_validate(raw) if isinstance(raw, dict) else None


def _power_pool_counts(code: Any) -> tuple[int | None, list[str] | None]:
    """Operators and data fields the way Power Pool counts them, from the expression itself."""
    try:
        tree = parse(code) if isinstance(code, str) else None
    except ParseError:
        tree = None
    if tree is None:
        return None, None
    return operator_count(tree), data_fields(tree)


def _info(alpha_id: str, body: dict[str, Any]) -> AlphaInfo:
    code = body.get("regular") or body.get("combo") or body.get("selection") or {}
    sample = body.get("is") or {}
    operators, fields = _power_pool_counts(code.get("code"))
    return AlphaInfo(
        alpha_id=alpha_id,
        type=body.get("type"),
        author=body.get("author"),
        code=code.get("code"),
        description=code.get("description"),
        operator_count=code.get("operatorCount"),
        name=body.get("name"),
        category=body.get("category"),
        color=body.get("color"),
        tags=[t for t in body.get("tags") or [] if isinstance(t, str)],
        stage=body.get("stage"),
        status=body.get("status"),
        date_created=body.get("dateCreated"),
        date_submitted=body.get("dateSubmitted"),
        date_modified=body.get("dateModified"),
        settings=body.get("settings") or {},
        classifications=[
            AlphaClassification.model_validate(c)
            for c in body.get("classifications") or []
            if isinstance(c, dict) and c.get("id") and c.get("name")
        ],
        in_sample=_stats(sample),
        investability=_stats(sample.get("investabilityConstrained")),
        checks=[c for c in sample.get("checks") or [] if isinstance(c, dict)],
        brain_url=f"{PLATFORM_ALPHA_URL}{alpha_id}",
        power_pool_operators=operators,
        data_fields=fields,
    )


def _columns(recordset: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    names = [p.get("name") for p in (recordset.get("schema") or {}).get("properties") or []]
    return [str(n) for n in names], [r for r in recordset.get("records") or [] if r]


def _series(recordset: dict[str, Any]) -> tuple[list[str], list[float | None], list[float | None]]:
    names, records = _columns(recordset)
    if "date" not in names:
        return [], [], []
    at = {name: i for i, name in enumerate(names)}

    def column(name: str) -> list[float | None]:
        i = at.get(name)
        return [r[i] if i is not None and i < len(r) else None for r in records]

    return (
        [str(r[at["date"]]) for r in records],
        column("pnl"),
        column("investability-constrained-pnl"),
    )


def _yearly(recordset: dict[str, Any]) -> list[AlphaYear]:
    names, records = _columns(recordset)
    return [
        AlphaYear.model_validate(dict(zip(names, r, strict=False)) | {"year": str(r[0])})
        for r in records
        if "year" in names
    ]


async def _lineage(state: State, alpha_id: str) -> AlphaLineage | None:
    """Where this Alpha came from locally: the task, lab and choices that produced it."""
    async with state.db.session() as session:
        record = await session.scalar(
            select(SimulationRecord)
            .where(SimulationRecord.alpha_id == alpha_id)
            .order_by(SimulationRecord.id)
            .limit(1)
        )
        trial = await session.scalar(
            select(Trial).where(Trial.alpha_id == alpha_id).order_by(Trial.id).limit(1)
        )
        study = await session.get(Study, trial.study_id) if trial is not None else None
        if study is None and record is not None:
            study = await session.scalar(select(Study).where(Study.task == record.task).limit(1))
        if record is None and study is None:
            return None

        siblings: list[LineageSibling] = []
        if study is not None:
            value = func.json_extract(Trial.values, "$[0]")
            rows = await session.execute(
                select(Trial.alpha_id, Trial.expression, value)
                .where(
                    Trial.study_id == study.id,
                    Trial.state == TrialState.COMPLETE,
                    Trial.alpha_id.is_not(None),
                    Trial.alpha_id != alpha_id,
                )
                .order_by(value.desc())
                .limit(5)
            )
            siblings = [
                LineageSibling(alpha_id=a, expression=e, value=float(v) if v is not None else None)
                for a, e, v in rows.tuples()
                if a
            ]

    sent = record.submitted_at if record is not None else None
    return AlphaLineage(
        task=study.task if study is not None else record.task if record is not None else "",
        lab=study.sampler if study is not None else None,
        lab_name=TASK_SAMPLERS.get(study.sampler) if study is not None else None,
        study_id=study.id if study is not None else None,
        study_name=study.name if study is not None else None,
        template_name=study.template_name if study is not None else None,
        params=dict(trial.params or {}) if trial is not None else {},
        generation=trial.generation if trial is not None else None,
        simulated_at=_iso(sent) if sent is not None else None,
        siblings=siblings,
    )


# -- routes --------------------------------------------------------------------


@router.get("/summary")
async def summary(state: State) -> BrainPayload:
    """Counts by stage and status — the shape of your pool at a glance."""
    return BrainPayload(await state.endpoints.alphas_summary())


@router.get("/{alpha_id}/page")
async def page(alpha_id: str, state: State, refresh: Refresh = False) -> AlphaView:
    """Everything the Alpha page shows, in one call.

    The Alpha is read from BRAIN on every open — its checks and properties change — and
    falls back to the last copy if BRAIN cannot answer. Its PnL and yearly series are fixed
    for the in-sample period, so they come from the cache unless ``refresh``.
    """
    problems: list[str] = []
    try:
        body, fetched = await _cached(
            state,
            f"alpha:{alpha_id}",
            lambda: state.endpoints.alpha_body(alpha_id),
            refresh=True,
        )
    except BrainError as exc:
        async with state.db.session() as session:
            stale = await session.get(BrainCache, f"alpha:{alpha_id}")
        if stale is None:
            raise
        body, fetched = stale.body, stale.fetched_at
        problems.append(
            f"BRAIN did not answer ({exc.message}); showing the copy from "
            f"{stale.fetched_at:%b %d, %H:%M} UTC."  # stored in UTC; unlabelled it read as local
        )

    dates: list[str] = []
    pnl: list[float | None] = []
    constrained: list[float | None] = []
    try:
        series, _ = await _cached(
            state,
            f"pnl:{alpha_id}",
            lambda: state.endpoints.recordset_body(alpha_id, "pnl"),
            refresh=refresh,
        )
        dates, pnl, constrained = _series(series)
    except BrainError as exc:
        problems.append(f"The PnL series could not be loaded: {exc.message}")

    yearly: list[AlphaYear] = []
    try:
        table, _ = await _cached(
            state,
            f"yearly:{alpha_id}",
            lambda: state.endpoints.recordset_body(alpha_id, "yearly-stats"),
            refresh=refresh,
        )
        yearly = _yearly(table)
    except BrainError as exc:
        problems.append(f"The yearly stats could not be loaded: {exc.message}")

    return AlphaView(
        alpha=_info(alpha_id, body),
        dates=dates,
        pnl=pnl,
        investability_pnl=constrained,
        yearly=yearly,
        lineage=await _lineage(state, alpha_id),
        fetched_at=_iso(fetched),
        problems=problems,
    )


@router.patch("/{alpha_id}")
async def update_properties(alpha_id: str, body: AlphaProperties, state: State) -> AlphaInfo:
    """Save the Alpha's name, category, colour, tags and description on BRAIN.

    Normalised the way the platform's own client sends them: a blank or ``NONE`` colour,
    a blank name or category become null; an empty description is left out.
    """
    blank = lambda v: v if v and v.strip() and v != "NONE" else None  # noqa: E731
    patch: dict[str, Any] = {
        "name": blank(body.name),
        "category": blank(body.category),
        "color": blank(body.color),
        "tags": [t.strip() for t in body.tags if t.strip()],
    }
    if body.description and body.description.strip():
        patch["regular"] = {"description": body.description}
    updated = await state.endpoints.update_alpha(alpha_id, patch)
    if not updated.get("id"):
        updated = await state.endpoints.alpha_body(alpha_id)
    async with state.db.session() as session:
        row = await session.get(BrainCache, f"alpha:{alpha_id}")
        if row is None:
            session.add(BrainCache(key=f"alpha:{alpha_id}", body=updated))
        else:
            row.body, row.fetched_at = updated, utcnow()
    return _info(alpha_id, updated)


@router.get("/{alpha_id}/check")
async def check_alpha(alpha_id: str, state: State) -> BrainPayload:
    """Re-run the submission checks without submitting.

    Changes nothing on the platform. It does update the local copy, so an alpha that has
    just resolved appears on the submit screen without waiting for the next backfill.
    """
    body = await state.endpoints.check_alpha(alpha_id)
    checks = ((body.get("is") or {}).get("checks")) or []
    if checks:
        await state.alphas.save_checks(alpha_id, checks)
    await _forget(state, f"alpha:{alpha_id}")
    return BrainPayload(body)


@router.get("/{alpha_id}/correlations/{kind}")
async def correlations(
    alpha_id: str,
    kind: CorrelationKind,
    state: State,
    refresh: Refresh = False,
    cached_only: CachedOnly = False,
) -> BrainPayload:
    """Correlation against your own submitted Alphas, the Power Pool, or production.

    A slow, rate-limited job, so the answer is kept and returned with ``fetchedAt``; only
    ``refresh`` asks BRAIN again. ``cachedOnly`` answers ``{"cached": false}`` when nothing
    is kept, so a page can show old answers without asking.
    """
    key = f"correlation:{kind}:{alpha_id}"
    if cached_only:
        return await _kept(state, key)
    body, fetched = await _cached(
        state,
        key,
        lambda: state.endpoints.correlations(alpha_id, kind),
        refresh=refresh,
    )
    return BrainPayload(body | {"cached": True, "fetchedAt": _iso(fetched)})


@router.get("/{alpha_id}/performance")
async def performance(
    alpha_id: str, state: State, refresh: Refresh = False, cached_only: CachedOnly = False
) -> BrainPayload:
    """Your pool's stats before and after this Alpha joins it, kept until refreshed."""
    key = f"performance:{alpha_id}"
    if cached_only:
        return await _kept(state, key)
    body, fetched = await _cached(
        state,
        key,
        lambda: state.endpoints.before_and_after(alpha_id),
        refresh=refresh,
    )
    return BrainPayload(body | {"cached": True, "fetchedAt": _iso(fetched)})
