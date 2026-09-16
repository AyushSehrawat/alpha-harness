"""Pyramids: a dataset category in one region and delay.

BRAIN pays a multiplier per pyramid and counts one as formulated once three Alphas are
submitted in it in a quarter. Both numbers come from the account's activity endpoints;
whether a pyramid can be researched here comes from the downloaded fields. BRAIN exposes
no quarter dates, and Genius levels run on calendar quarters, so the quarter is derived
from today's date in platform time.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..brain.filters import PLATFORM_TZ

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..brain.endpoints import BrainEndpoints
    from ..db.duck import Catalog

#: Alphas submitted in a pyramid this quarter before it counts as formulated.
LIT_AT = 3

MULTIPLIERS_TTL = 6 * 3600
COUNTS_TTL = 600

# In-process rather than persisted: a restart costs two cheap GETs.
_cache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def quarter_start(today: date) -> date:
    return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)


def next_quarter_start(today: date) -> date:
    start = quarter_start(today)
    return date(start.year + 1, 1, 1) if start.month == 10 else date(start.year, start.month + 3, 1)


def assemble(
    multipliers: list[dict[str, Any]],
    counts: list[dict[str, Any]],
    synced: set[tuple[str, str, int]],
    universes: dict[str, int],
) -> dict[str, Any]:
    """Merge the two activity lists and the download state into one grid.

    The multipliers decide which pyramids exist: they are the ones BRAIN will pay for, and
    they match the markets the account can simulate. Submitted-alpha counts only fill in
    pyramids already on the grid — that list also carries regions the account has no access
    to, which would otherwise show as columns nothing can ever be researched in.
    """
    cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    categories: dict[str, str] = {}

    def cell(item: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
        category = item.get("category") or {}
        category_id, region, delay = category.get("id"), item.get("region"), item.get("delay")
        if not category_id or not region or delay is None:
            return None
        key = (category_id, region, int(delay))
        if not create:
            return cells.get(key)
        categories.setdefault(category_id, category.get("name") or category_id)
        return cells.setdefault(
            key,
            {
                "categoryId": category_id,
                "region": region,
                "delay": int(delay),
                "multiplier": None,
                "alphaCount": 0,
            },
        )

    for item in multipliers:
        if (found := cell(item, create=True)) is not None:
            found["multiplier"] = item.get("multiplier")
    for item in counts:
        if (found := cell(item, create=False)) is not None:
            found["alphaCount"] = int(item.get("alphaCount") or 0)

    for key, found in cells.items():
        found["lit"] = found["alphaCount"] >= LIT_AT
        found["synced"] = key in synced

    # Widest markets first, then alphabetically — the ordering the sync matrix uses, so the
    # two screens read the same way whatever regions an account has.
    columns = sorted(
        {(c["region"], c["delay"]) for c in cells.values()},
        key=lambda column: (-universes.get(column[0], 0), column[0], column[1]),
    )
    return {
        "columns": [{"region": region, "delay": delay} for region, delay in columns],
        "categories": [{"id": cid, "name": name} for cid, name in categories.items()],
        "cells": list(cells.values()),
    }


async def pyramid_grid(
    endpoints: BrainEndpoints, catalog: Catalog, today: date | None = None
) -> dict[str, Any]:
    today = today or datetime.now(PLATFORM_TZ).date()
    start, end = quarter_start(today), next_quarter_start(today)
    multipliers = await _cached("multipliers", MULTIPLIERS_TTL, endpoints.pyramid_multipliers)
    counts = await _cached(
        f"alphas:{start}",
        COUNTS_TTL,
        lambda: endpoints.pyramid_alphas(start.isoformat(), end.isoformat()),
    )
    rows = await catalog.query(
        "SELECT DISTINCT category_id, region, delay FROM data_field WHERE category_id IS NOT NULL"
    )
    synced = {(r["category_id"], r["region"], int(r["delay"])) for r in rows}
    widths = await catalog.query(
        "SELECT region, count(DISTINCT universe) AS universes FROM data_field GROUP BY region"
    )
    universes = {r["region"]: int(r["universes"]) for r in widths}
    return assemble(multipliers, counts, synced, universes) | {
        "quarter": {"start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat()}
    }
