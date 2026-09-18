"""BRAIN's performance figures, rebuilt from an alpha's ``pnl`` and ``turnover`` recordsets.

Measured against 36 alphas across nine regions; each figure matches the platform's to the
decimals it shows, apart from $1 PnL differences that come from the recordsets being rounded.
What that measurement settled, where it departs from the documentation:

- A year is **250** days, not the documented 252, for both Sharpe and returns.
- Daily PnL is the difference of the cumulative ``pnl`` recordset. The ``daily-pnl`` recordset
  is rounded separately and drifts from the platform's own figures.
- Each ``turnover`` row is stamped one trading day early. The day the book is first built
  trades it whole (1.0) and can fall off that shift, and a gap in the series after trading
  began is the book being sold whole (1.0 again).
- Drawdown's running peak starts at the first day's cumulative PnL, not at zero.
- Fitness is computed from the Sharpe as displayed, rounded to two places.

- BRAIN's IS and test blocks include final days no recordset carries (see
  :func:`closing_days`). The yearly rows leave them out.
- For some Alphas the turnover recordset is not the turnover BRAIN's figures use; its yearly
  figures are, so turnover is scaled to them (see :func:`calibrate`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Sequence

type Floats = npt.NDArray[np.float64]

BOOK = 20_000_000.0
YEAR = 250
#: BRAIN's fitness floors turnover here so a near-idle alpha is not rewarded without bound.
FITNESS_TURNOVER_FLOOR = 0.125
#: Half the last decimal of BRAIN's four-decimal turnover.
ROUNDING = 0.00005


@dataclass(frozen=True, slots=True)
class Stats:
    pnl: float
    returns: float
    turnover: float
    drawdown: float
    margin: float
    #: ``None`` below two days, where a spread is undefined.
    sharpe: float | None
    fitness: float | None
    days: int


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value[:10])
    return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def daily_rows(
    pnl_rows: Sequence[dict[str, Any]], turnover_rows: Sequence[dict[str, Any]]
) -> list[tuple[date, float, float]]:
    """``(date, pnl, turnover)`` per trading day, from the two recordsets' rows by column name."""
    cumulative = [
        (day, value)
        for row in pnl_rows
        if (day := _day(row.get("date"))) is not None
        and (value := _number(row.get("pnl"))) is not None
    ]
    if not cumulative:
        return []
    recorded = {
        day: _number(row.get("turnover"))
        for row in turnover_rows
        if (day := _day(row.get("date"))) is not None
    }
    first_value = next((v for v in recorded.values() if v is not None), None)
    first_day = cumulative[0][0]

    out: list[tuple[date, float, float]] = []
    previous_cum = 0.0
    holding = False
    for i, (day, cum) in enumerate(cumulative):
        if i == 0:
            built = recorded.get(first_day) is not None and first_value != 1.0
            turnover = 1.0 if built else 0.0
            holding = built
        else:
            value = recorded.get(cumulative[i - 1][0])
            if value is None:
                turnover = 1.0 if holding else 0.0
                holding = False
            else:
                turnover = value
                holding = True
        out.append((day, cum - previous_cum, turnover))
        previous_cum = cum
    return out


def calibrate(
    days: Sequence[tuple[date, float, float]],
    yearly: Sequence[dict[str, Any]],
    split: date | None,
) -> list[tuple[date, float, float]]:
    """Scale each ``yearly-stats`` segment's turnover to the figure BRAIN reports for it.

    For delay-0 Alphas with a group (or no) neutralization, and for CHN and GLB, the turnover
    recordset differs from the turnover BRAIN's own figures use, by 0.1 to 20%, spread through
    every year and not recoverable day by day. Its yearly figures are exact, so each year (the
    split year cut at the split day, as BRAIN cuts it) is scaled to match. Measured on 155 Alphas
    across 15 region and delay pairs: IS, train and test then match BRAIN on 98% of figures,
    the rest four-decimal rounding.
    """
    turnover = [t for _, _, t in days]
    for row in yearly:
        target = _number(row.get("turnover"))
        year = int(str(row.get("year")))
        if target is None:
            continue
        test = row.get("stage") == "TEST"
        segment = [
            i
            for i, (d, _, _) in enumerate(days)
            if d.year == year and (split is None or year != split.year or (d >= split) == test)
        ]
        have = sum(turnover[i] for i in segment)
        # BRAIN rounds its figure to four decimals: a segment already within that is exact, and
        # scaling it to the rounded figure would only add the rounding.
        if segment and have > 0 and abs(have / len(segment) - target) > ROUNDING:
            for i in segment:
                turnover[i] *= target * len(segment) / have
    return [(d, p, t) for (d, p, _), t in zip(days, turnover, strict=True)]


def closing_days(
    days: Sequence[tuple[date, float, float]],
    *,
    end: date,
    is_pnl: float | None,
    split: date | None,
    test_turnover: float | None,
) -> list[tuple[date, float, float]]:
    """The final days BRAIN counts in an Alpha's IS and test blocks but exports in no recordset:
    every trading day between the last recordset row and the simulation's end, plus one more.

    Only their total is known, so it is split evenly. PnL comes from the reported IS PnL;
    turnover from the reported test turnover, a mean that includes them, so it is only as exact
    as that figure's four decimals allow.
    """
    if is_pnl is None or not days or end <= days[-1][0]:
        return []
    last = days[-1][0]
    between = [
        d
        for d in (last + timedelta(days=k) for k in range(1, (end - last).days + 1))
        if d.weekday() < 5
    ]
    # The extra day falls on the end date, or on the next weekday when the end is itself one.
    after = end + timedelta(days=1)
    while after.weekday() >= 5:
        after += timedelta(days=1)
    stamps = [*between, after if end in between else end]
    count = len(stamps)
    pnl = (is_pnl - sum(p for _, p, _ in days)) / count
    turnover = 0.0
    if split is not None and test_turnover is not None:
        tested = [t for d, _, t in days if d >= split]
        total = test_turnover * (len(tested) + count) - sum(tested)
        turnover = min(2.0, max(0.0, total / count))
    return [(d, pnl, turnover) for d in stamps]


def with_closing(
    rows: list[tuple[date, float, float]], info: dict[str, Any]
) -> list[tuple[date, float, float]]:
    """An Alpha's recordset days, oldest first, plus its :func:`closing_days`. ``info`` is its
    stored row: ``end_date``, ``is_pnl``, ``test_start`` and ``test_turnover``."""
    end = info.get("end_date")
    if not isinstance(end, date):
        return rows
    return [
        *rows,
        *closing_days(
            rows,
            end=end,
            is_pnl=info.get("is_pnl"),
            split=info.get("test_start"),
            test_turnover=info.get("test_turnover"),
        ),
    ]


def stats(pnl: Floats, turnover: Floats, *, trim: bool = True) -> Stats | None:
    """The platform's stat block over these days. ``trim`` drops the idle days before the first
    trade, as BRAIN's IS, train and test blocks do; its yearly rows keep them."""
    if trim:
        active = np.flatnonzero((pnl != 0) | (turnover != 0))
        if not active.size:
            return None
        pnl, turnover = pnl[active[0] :], turnover[active[0] :]
    days = int(pnl.size)
    if not days:
        return None

    total = float(pnl.sum())
    traded = float(turnover.sum())
    returns = float(pnl.mean()) * YEAR / (BOOK / 2)
    mean_turnover = traded / days
    cum = np.cumsum(pnl)
    drawdown = float((np.maximum.accumulate(cum) - cum).max()) / (BOOK / 2)
    margin = total / (traded * BOOK) if traded else 0.0

    spread = float(pnl.std(ddof=1)) if days > 1 else 0.0
    sharpe = float(pnl.mean()) / spread * math.sqrt(YEAR) if spread > 0 else None
    fitness = (
        round(sharpe, 2) * math.sqrt(abs(returns) / max(mean_turnover, FITNESS_TURNOVER_FLOOR))
        if sharpe is not None
        else None
    )
    return Stats(total, returns, mean_turnover, drawdown, margin, sharpe, fitness, days)


def windows(
    dates: Sequence[date], pnl: Floats, turnover: Floats, split: date | None
) -> dict[str, Stats | None]:
    """IS, train and test blocks. Train and test both include the split day, as BRAIN's do."""
    stamps = np.array(dates, dtype="datetime64[D]")
    out: dict[str, Stats | None] = {"in_sample": stats(pnl, turnover)}
    if split is None:
        out["train"] = out["test"] = None
        return out
    cut = np.datetime64(split, "D")
    train, test = stamps <= cut, stamps >= cut
    out["train"] = stats(pnl[train], turnover[train])
    out["test"] = stats(pnl[test], turnover[test])
    return out


def yearly(
    dates: Sequence[date], pnl: Floats, turnover: Floats, split: date | None
) -> list[tuple[int, str, Stats]]:
    """BRAIN's yearly rows. The split year is cut in two at the split day."""

    def stage(day: date) -> str:
        if split is None:
            return "IS"
        return "TEST" if day >= split else "TRAIN"

    groups = [(d.year, stage(d)) for d in dates]
    rows: list[tuple[int, str, Stats]] = []
    for year, name in dict.fromkeys(groups):
        mask = np.array([g == (year, name) for g in groups])
        found = stats(pnl[mask], turnover[mask], trim=False)
        if found is not None:
            rows.append((year, name, found))
    return rows


def after_cost(pnl: Floats, turnover: Floats, bps: float) -> Floats:
    """Daily PnL less an estimated trading cost of ``bps`` on every dollar traded."""
    return pnl - turnover * BOOK * bps / 10_000
