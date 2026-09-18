"""Self-check: past the grid limit the Portfolio sends the most correlated pairs, not the grid,
and they are exactly the top of the grid it would have sent.

Run as a script -- ``uv run python tests/portfolio_check.py``.
"""

from __future__ import annotations

import sys
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alpha_harness.tools import portfolio, submission_planner


def series(n: int) -> dict[str, dict[date, tuple[float, float]]]:
    """``n`` Alphas over 1,200 weekdays: a shared factor in varying doses, so pairs differ."""
    rng = np.random.default_rng(5)
    days = [d for d in (date(2020, 1, 1) + timedelta(k) for k in range(1700)) if d.weekday() < 5]
    common = rng.normal(size=len(days))
    out = {}
    for i in range(n):
        own = rng.normal(size=len(days)) + common * rng.uniform(0, 1.5)
        out[f"a{i:02}"] = {d: (float(v), 0.5) for d, v in zip(days, own, strict=True)}
    return out


small = portfolio.compute(series(portfolio.GRID_LIMIT), {}, list(series(portfolio.GRID_LIMIT)), 0)
assert len(small["correlation"]) == portfolio.GRID_LIMIT and small["top_pairs"] == [], small.keys()

data = series(40)
ids = list(data)
big = portfolio.compute(data, {}, ids, 0)
assert big["correlation"] == [], "the grid was sent past the limit"
assert big["measured_pairs"] == 40 * 39 // 2, big["measured_pairs"]

# The grid it would have sent, from the same inputs, and its top pairs.
kept, dates, grid = submission_planner.grid(
    {a: {d: v[0] for d, v in s.items()} for a, s in data.items()}, ids
)
rho = submission_planner.correlations(
    grid[bisect_left(dates, submission_planner.window_start(dates[-1])) :]
)
want = sorted(
    ((rho[i, j], kept[i], kept[j]) for i in range(40) for j in range(i + 1, 40)), reverse=True
)[: portfolio.TOP_PAIRS]
got = [(p["correlation"], p["a"], p["b"]) for p in big["top_pairs"]]
assert [(a, b) for _, a, b in got] == [(a, b) for _, a, b in want], (got[:3], want[:3])
assert all(
    abs(g - round(float(w), 4)) < 1e-12 for (g, _, _), (w, _, _) in zip(got, want, strict=True)
)
assert big["highest"]["correlation"] == max(v for v, _, _ in want), big["highest"]

limit, top = portfolio.GRID_LIMIT, portfolio.TOP_PAIRS
print(f"ok: {limit} Alphas send the grid, 40 send the top {top} of 780 pairs")

# Past the limit with nothing measurable: no two Alphas share MIN_OVERLAP days.
start = date(2015, 1, 1)
apart = {
    f"s{i:02}": {start + timedelta(days=90 * i + k): (float(k % 7 - 3), 0.5) for k in range(60)}
    for i in range(31)
}
empty = portfolio.compute(apart, {}, list(apart), 0)
assert empty["correlation"] == [] and empty["top_pairs"] == [], empty["top_pairs"]
assert empty["measured_pairs"] == 0 and empty["highest"] is None, empty["highest"]

# Unmeasurable pairs mixed with negative ones: NaN never ranks, and the order is by value, so
# -0.05 comes before -0.80.
rng = np.random.default_rng(9)
days = [d for d in (date(2020, 1, 1) + timedelta(k) for k in range(1700)) if d.weekday() < 5]
common = rng.normal(size=len(days))
mixed: dict[str, dict[date, tuple[float, float]]] = {}
for i in range(35):
    loading = rng.uniform(-1.5, 1.5)
    values = rng.normal(size=len(days)) + common * loading
    span = days[-100:] if i >= 30 else days  # the last five are too short to measure
    mixed[f"m{i:02}"] = {d: (float(v), 0.5) for d, v in zip(days, values, strict=True) if d in span}
result = portfolio.compute(mixed, {}, list(mixed), 0)
pairs = result["top_pairs"]
values = [p["correlation"] for p in pairs]
assert values == sorted(values, reverse=True), values
assert not any(p["a"] >= "m30" or p["b"] >= "m30" for p in pairs), "an unmeasurable pair ranked"
assert result["measured_pairs"] == 30 * 29 // 2, result["measured_pairs"]
kept, dates, grid = submission_planner.grid(
    {a: {d: v[0] for d, v in s.items()} for a, s in mixed.items()}, list(mixed)
)
rho = submission_planner.correlations(
    grid[bisect_left(dates, submission_planner.window_start(dates[-1])) :]
)
finite = sorted(
    (
        round(float(rho[i, j]), 4)
        for i in range(35)
        for j in range(i + 1, 35)
        if not np.isnan(rho[i, j])
    ),
    reverse=True,
)
assert values == finite[: portfolio.TOP_PAIRS], (values[:3], finite[:3])
assert min(finite) < 0, "the fixture has no negative pair, so it proves nothing about them"
print(
    f"ok: 0 measurable pairs sends none; NaN never ranks; lowest pair {min(finite):.2f} sorts last"
)

# The whole ranking, not only its positive head: with the cut lifted, every measurable pair
# comes back in value order, negatives included.
portfolio.TOP_PAIRS = 10_000
every = [p["correlation"] for p in portfolio.compute(mixed, {}, list(mixed), 0)["top_pairs"]]
assert every == finite and every[-1] < 0 < every[0], (every[:2], every[-2:])
print(f"ok: all {len(every)} pairs in value order, {sum(v < 0 for v in every)} of them negative")
