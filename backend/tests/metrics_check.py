"""Self-check: BRAIN's stat block rebuilt from the pnl and turnover recordsets.

Run as a script -- ``uv run python tests/metrics_check.py``. Each fixture is built so a
wrong rule changes the answer: the first trading day is a loss (a peak starting at zero
doubles the drawdown), and the turnover series has a gap mid-way (a liquidation).
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alpha_harness.vault import metrics

start = date(2020, 1, 6)
days = [start + timedelta(days=i) for i in range(6)]
cumulative = [0.0, 0.0, -100.0, -50.0, 150.0, 100.0]
# One row fewer than PnL, each stamped a day early; None mid-way is the book sold whole.
recorded = [None, 1.0, 0.5, None, 0.4]

rows = metrics.daily_rows(
    [
        {"date": d.isoformat(), "pnl": c, "investability-constrained-pnl": 7.0}
        for d, c in zip(days, cumulative, strict=True)
    ],
    [{"date": d.isoformat(), "turnover": t} for d, t in zip(days, recorded, strict=False)],
)
pnl = np.array([r[1] for r in rows])
turnover = np.array([r[2] for r in rows])
assert pnl.tolist() == [0.0, 0.0, -100.0, 50.0, 200.0, -50.0], pnl
assert turnover.tolist() == [0.0, 0.0, 1.0, 0.5, 1.0, 0.4], turnover

got = metrics.stats(pnl, turnover)
assert got is not None
active = [-100.0, 50.0, 200.0, -50.0]
mean = sum(active) / 4
spread = math.sqrt(sum((x - mean) ** 2 for x in active) / 3)
sharpe = mean / spread * math.sqrt(250)
returns = mean * 250 / 10_000_000
mean_turnover = 2.9 / 4
assert got.days == 4, got
assert math.isclose(got.sharpe or 0, sharpe), (got.sharpe, sharpe)
assert math.isclose(got.returns, returns), got
assert math.isclose(got.turnover, mean_turnover), got
# Peak starts at the first day's -100, so the worst fall is 150 -> 100, not 0 -> -100.
assert math.isclose(got.drawdown, 50 / 10_000_000), got
assert math.isclose(got.margin, 100 / (2.9 * 20_000_000)), got
assert math.isclose(got.fitness or 0, round(sharpe, 2) * math.sqrt(abs(returns) / mean_turnover)), (
    got
)


def first_turnover(first: float | None) -> float:
    rows = metrics.daily_rows(
        [{"date": d.isoformat(), "pnl": 1.0} for d in days[:2]],
        [{"date": days[0].isoformat(), "turnover": first}],
    )
    return rows[0][2]


# The first build trades the whole book; it only needs adding when the shift dropped it.
assert first_turnover(0.8) == 1.0
assert first_turnover(1.0) == 0.0
assert first_turnover(None) == 0.0

split = days[3]
blocks = metrics.windows(days, pnl, turnover, split)
assert blocks["train"] is not None and blocks["train"].days == 2, blocks
assert blocks["test"] is not None and blocks["test"].days == 3, blocks
assert [(y, s, r.days) for y, s, r in metrics.yearly(days, pnl, turnover, split)] == [
    (2020, "TRAIN", 3),
    (2020, "TEST", 3),
]

# The final days BRAIN counts but never exports: every trading day after the last row up to the
# end date, plus one. The last row is Saturday 11 Jan and the end Wednesday 15 Jan, so Mon-Wed
# are missing and the extra day, with the end itself a trading day, lands on Thursday.
extra = metrics.closing_days(
    rows, end=days[-1] + timedelta(days=4), is_pnl=150.0, split=days[3], test_turnover=0.6
)
assert [d for d, _, _ in extra] == [date(2020, 1, d) for d in (13, 14, 15, 16)], extra
assert all(math.isclose(p, (150.0 - 100.0) / 4) for _, p, _ in extra), extra
# Test days hold 0.5, 1.0 and 0.4; a mean of 0.6 over seven days needs 2.3 more, split four ways.
assert all(math.isclose(t, (0.6 * 7 - 1.9) / 4) for _, _, t in extra), extra
assert metrics.closing_days(rows, end=days[-1], is_pnl=150.0, split=None, test_turnover=None) == []
# A Friday end puts the extra day on the next Monday, never on a weekend.
friday = metrics.closing_days(
    [(date(2023, 12, 28), 1.0, 0.5)],
    end=date(2023, 12, 29),
    is_pnl=5.0,
    split=None,
    test_turnover=None,
)
assert [d for d, _, _ in friday] == [date(2023, 12, 29), date(2024, 1, 1)], friday

# Each yearly-stats segment scales to BRAIN's figure; the split year is cut at the split day.
scaled = metrics.calibrate(
    rows,
    [
        {"year": 2020, "stage": "TRAIN", "turnover": 0.3},
        {"year": 2020, "stage": "TEST", "turnover": 0.5},
    ],
    days[3],
)
assert math.isclose(sum(t for d, _, t in scaled if d < days[3]) / 3, 0.3), scaled
assert math.isclose(sum(t for d, _, t in scaled if d >= days[3]) / 3, 0.5), scaled

print(f"ok: Sharpe {got.sharpe:.4f}, drawdown {got.drawdown:.7f}, fitness {got.fitness:.4f}")
