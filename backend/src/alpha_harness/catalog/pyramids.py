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
#: The order BRAIN lists regions in. Anything else follows alphabetically.
REGION_ORDER = ("USA", "GLB", "EUR", "ASI", "CHN", "JPN", "IND", "DEU", "GBR")

MULTIPLIERS_TTL = 6 * 3600
COUNTS_TTL = 600

# ponytail: in-process cache; a restart costs two cheap GETs. Move to MetadataCache if
# the multipliers start being read somewhere startup-sensitive.
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
) -> dict[str, Any]:
    """Merge the two activity lists and the download state into one grid."""
    cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    categories: dict[str, str] = {}

    def cell(item: dict[str, Any]) -> dict[str, Any] | None:
        category = item.get("category") or {}
        category_id, region, delay = category.get("id"), item.get("region"), item.get("delay")
        if not category_id or not region or delay is None:
            return None
        categories.setdefault(category_id, category.get("name") or category_id)
        key = (category_id, region, int(delay))
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
        if (found := cell(item)) is not None:
            found["multiplier"] = item.get("multiplier")
    for item in counts:
        if (found := cell(item)) is not None:
            found["alphaCount"] = int(item.get("alphaCount") or 0)

    for key, found in cells.items():
        found["lit"] = found["alphaCount"] >= LIT_AT
        found["synced"] = key in synced

    columns = sorted({(c["region"], c["delay"]) for c in cells.values()}, key=_column_order)
    return {
        "columns": [{"region": region, "delay": delay} for region, delay in columns],
        "categories": [{"id": cid, "name": name} for cid, name in categories.items()],
        "cells": list(cells.values()),
    }


def _column_order(column: tuple[str, int]) -> tuple[int, str, int]:
    region, delay = column
    rank = REGION_ORDER.index(region) if region in REGION_ORDER else len(REGION_ORDER)
    return rank, region, delay


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
    return assemble(multipliers, counts, synced) | {
        "quarter": {"start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat()}
    }
