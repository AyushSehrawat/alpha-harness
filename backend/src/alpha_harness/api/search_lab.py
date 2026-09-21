"""Search Lab: choose datasets, cores and simulations, then run the search as a task.

A task waits in Tasks for free cores and may use more than one day's allowance before it
is done. ``/quick`` is the Dashboard's one click: a focused run of today's simulations, now.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..brain.errors import BrainError
from ..brain.schemas import REGION_AGNOSTIC_REGION
from ..catalog.pyramids import pyramid_grid
from ..db.models import Study, utcnow
from ..labs import search
from ..labs.launch import (
    AddedTask,
    FieldCounts,
    LeftOut,
    OperatorsRead,
    SampleAlpha,
    SearchRequest,
    account_operators,
    add_study,
    field_counts,
    market_for,
    operators_read,
    preview_samples,
    startup_trials,
)
from ..labs.params import SEARCH_SAMPLER, SearchParams
from ..schemas import Out
from .deps import State, refuse
from .today import simulations_today

router = APIRouter(prefix="/api/search-lab", tags=["search-lab"])


class Options(Out):
    operators: OperatorsRead
    cross_sectional: list[str]
    time_series: list[str]
    group: list[str]
    vector: list[str]
    lookbacks: list[int]
    groups: list[str]
    decays: list[int]
    truncation: float
    max_cores: int
    max_simulations: int


class Preview(Out):
    round: int
    fields: FieldCounts
    left_out: LeftOut
    universes: list[str]
    neutralizations: list[str]
    families: list[str]
    sample: list[SampleAlpha]
    problems: list[str]
    warnings: list[str]


class QuickRequest(BaseModel):
    """The market to run in, and the datasets last chosen there; none means the pyramids."""

    region: str = "USA"
    delay: int = Field(default=1, ge=0, le=1)
    universe: str | None = "TOP3000"
    dataset_ids: list[str] = Field(default_factory=list, max_length=200)
    #: How many of today's simulations to run; none means :data:`DEFAULT_RUN`.
    simulations: int | None = Field(default=None, ge=1)


#: A focused run that finishes in about half an hour over eight cores, rather than hours.
DEFAULT_RUN = 500

#: Cores per quick-run task, so several of them run side by side rather than one
#: holding the whole engine.
QUICK_CORES = 4


class QuickTask(Out):
    id: int
    name: str
    dataset_ids: list[str]
    simulations: int


class QuickRun(Out):
    tasks: list[QuickTask]
    simulations: int


@router.get("/options")
async def options(state: State, refresh: bool = False) -> Options:
    operators = await account_operators(state, refresh=refresh)
    cat = search.catalogue(operators)
    return Options.model_validate(
        {
            "operators": operators_read(operators),
            "crossSectional": list(cat.cs),
            "timeSeries": list(cat.ts),
            "group": list(cat.group),
            "vector": list(cat.vector),
            "lookbacks": list(search.LOOKBACKS),
            "groups": list(search.GROUPS),
            "decays": list(search.DECAYS),
            "truncation": search.TRUNCATION,
            "maxCores": search.MAX_CORES,
            "maxSimulations": search.MAX_SIMULATIONS,
        }
    )


async def _plan(body: SearchRequest, state: Any) -> dict[str, Any]:
    """Everything a task would search, checked, without queueing anything."""
    market = await market_for(body, state)
    problems, pool = market["problems"], market["pool"]
    cat = search.catalogue(market["operators"])
    shapes = search.families(cat)
    if market["operators"] and not shapes:
        problems.append("None of your operators fit a search shape.")

    space = {
        "fields": pool.fields,
        "families": list(shapes),
        "cs": list(cat.cs),
        "ts": list(cat.ts),
        "group": list(cat.group),
        "vector": market["vector"],
        "lookbacks": list(search.LOOKBACKS),
        "groups": list(search.GROUPS),
        "universes": list(pool.universes),
        "absent": pool.absent,
        "neutralizations": market["neutralizations"],
    }
    sample: list[SampleAlpha] = []
    if not problems:
        run = SearchParams(region=body.region, delay=body.delay, decay=body.decay, space=space)
        choices = search.field_choices(space)
        sample = preview_samples(
            lambda rng: search.request_for(
                search.suggest(search.RandomTrial(rng), space, choices), run
            ),
            tries=5,
        )

    return {
        "round": body.cores * 10,
        "fields": field_counts(pool.fields),
        "leftOut": {"vector": pool.vector_skipped},
        "universes": list(pool.universes),
        "neutralizations": market["neutralizations"],
        "families": list(shapes),
        "sample": sample,
        "problems": problems,
        "warnings": market["warnings"],
        "space": space,
    }


@router.post("/preview")
async def preview(body: SearchRequest, state: State) -> Preview:
    """What a task would search. Free; queues nothing."""
    return Preview.model_validate(await _plan(body, state))


@router.post("/tasks", status_code=201)
async def add_task(body: SearchRequest, state: State, run: bool = False) -> AddedTask:
    """Add the search to Tasks: queued when ``run``, else not started and spending nothing."""
    if body.simulations < 1:
        raise refuse(422, "no_simulations", "Assign the simulations for this task.")
    plan = await _plan(body, state)
    if plan["problems"]:
        raise refuse(422, "search_blocked", plan["problems"][0])
    row = await _create(body, plan, state, run=run)
    return AddedTask(id=row.id, name=row.name)


@router.post("/quick", status_code=201)
async def quick(body: QuickRequest, state: State) -> QuickRun:
    """Run some of today's unclaimed simulations now, split across tasks that run side by side.

    :data:`DEFAULT_RUN` unless the body asks for more, never beyond what is left. The
    datasets are the ones last chosen, else every synced dataset in a pyramid not yet
    formulated this quarter, highest multiplier first. Nothing is added unless all of it can run.
    """
    left = (await simulations_today(state))["unspoken"]
    if left < 1:
        raise refuse(409, "nothing_left", "Today's simulations are already running or queued.")
    if body.region == REGION_AGNOSTIC_REGION:
        # The guided path stays on one region: region-agnostic alphas are submittable only
        # where two regions hold up, which is not a bar to put a first run behind.
        raise refuse(
            422,
            "region_agnostic_quick_run",
            "All regions at once is a Search Lab choice, not a one-click run. Open Search Lab "
            "to start one.",
        )
    ranked = body.dataset_ids or await open_pyramid_datasets(state, body.region, body.delay)
    if not ranked:
        raise refuse(
            422,
            "no_datasets",
            f"No synced {body.region} delay {body.delay} dataset is in a pyramid still open "
            "this quarter. Sync one in Pyramids, or choose datasets in Search Lab.",
        )

    budget = min(left, body.simulations or DEFAULT_RUN)
    count = max(1, min(len(ranked), state.engine.slots // QUICK_CORES, budget))
    share, extra = divmod(min(budget, search.MAX_SIMULATIONS * count), count)
    planned: list[tuple[SearchRequest, dict[str, Any]]] = []
    for i in range(count):
        request = SearchRequest(
            region=body.region,
            delay=body.delay,
            universe=body.universe,
            cores=QUICK_CORES,
            # Dealt in turn, so every task gets some of the best pyramids.
            dataset_ids=ranked[i::count][:200],
            simulations=share + (1 if i < extra else 0),
        )
        plan = await _plan(request, state)
        if plan["problems"]:
            raise refuse(422, "search_blocked", plan["problems"][0])
        planned.append((request, plan))

    rows = [await _create(request, plan, state, run=True) for request, plan in planned]
    return QuickRun(
        tasks=[
            QuickTask(
                id=row.id,
                name=row.name,
                dataset_ids=request.dataset_ids,
                simulations=request.simulations,
            )
            for row, (request, _) in zip(rows, planned, strict=True)
        ],
        simulations=sum(request.simulations for request, _ in planned),
    )


async def open_pyramid_datasets(state: Any, region: str, delay: int) -> list[str]:
    """Synced datasets in the market's unformulated pyramids, highest multiplier first."""
    try:
        grid = await pyramid_grid(state.endpoints, state.catalog)
    except BrainError as exc:
        raise refuse(502, "pyramids_unread", f"BRAIN's pyramids could not be read: {exc}") from exc
    cells = [
        c
        for c in grid["cells"]
        if c["region"] == region and c["delay"] == delay and c["synced"] and not c["lit"]
    ]
    cells.sort(key=lambda c: -(c["multiplier"] or 0))
    rank = {c["categoryId"]: i for i, c in enumerate(cells)}
    rows = await state.catalog.query(
        "SELECT DISTINCT dataset_id, category_id FROM data_field "
        "WHERE region = ? AND delay = ? AND dataset_id IS NOT NULL",
        [region, delay],
    )
    held = sorted(
        (rank[r["category_id"]], str(r["dataset_id"])) for r in rows if r["category_id"] in rank
    )
    return list(dict.fromkeys(dataset for _, dataset in held))


async def _create(body: SearchRequest, plan: dict[str, Any], state: Any, *, run: bool) -> Study:
    """Store a planned task, queued for the scheduler when ``run``."""
    now = utcnow()
    per_round, size = plan["round"], body.simulations
    return await add_study(
        state,
        now=now,
        lab="Search Lab",
        prefix="search",
        sampler=SEARCH_SAMPLER,
        params=SearchParams(
            space=plan["space"],
            region=body.region,
            delay=body.delay,
            decay=body.decay,
            cores=body.cores,
            dataset_ids=body.dataset_ids,
            n_startup_trials=startup_trials(len(plan["space"]["fields"]), size, per_round),
            queued_at=now.isoformat() if run else None,
        ),
        objective="train_sharpe",
        simulations=size,
        batch_size=per_round,
        template_source="# Search Lab writes its own expressions; there is no template.",
        run=run,
    )
