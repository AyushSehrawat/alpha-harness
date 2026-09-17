"""Self-check: the Submission Planner builds a real portfolio, not one bet repeated.

Run as a script -- ``uv run python tests/submission_planner_check.py``. The fixture is three
independent factors wearing five near-duplicate disguises each, which is the shape the real
candidate set has: 278 Alphas spanning about five and a half independent directions.
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alpha_harness.labs.study import submittable
from alpha_harness.tools import submission_planner as planner
from alpha_harness.vault.yields import is_promising, is_submittable

BLOCKS, PER_BLOCK, DAYS = 3, 5, 6000


def fixture() -> dict[str, dict[date, float]]:
    """Three uncorrelated factors, five noisy copies of each, all at a Sharpe near 1.

    The drift matters: with zero-mean noise every Sharpe is 0, nothing combines, and the
    check passes while measuring nothing.
    """
    rng = np.random.default_rng(0)
    drift = 1.0 / np.sqrt(planner.TRADING_DAYS)
    base = rng.normal(loc=drift, size=(DAYS, BLOCKS))
    start = date(2014, 1, 1)
    days = [start + timedelta(days=i) for i in range(DAYS)]
    out: dict[str, dict[date, float]] = {}
    for block in range(BLOCKS):
        for copy in range(PER_BLOCK):
            series = base[:, block] + rng.normal(scale=0.25, size=DAYS)
            out[f"b{block}c{copy}"] = dict(zip(days, series.tolist(), strict=True))
    return out


def check_search(days: dict[str, dict[date, float]]) -> None:
    """``search`` ranks by Sharpe, and only returns portfolios BRAIN would accept.

    The ranking is the part worth pinning. ``search`` scores a candidate from running sums and
    a one-pass variance, leaning on ``scaled`` being exactly zero on days nobody traded so the
    sums need no masking; ``sharpe`` masks first and uses numpy's two-pass ``std``. If that
    reasoning is wrong the two disagree, and the search silently ranks by something that is not
    a Sharpe ratio -- the portfolio would still look plausible, so nothing else here would
    catch it.
    """
    kept, _dates, full = planner.grid(days, sorted(days))
    rho = planner.correlations(full)
    scaled, have = planner.scale(full), ~np.isnan(full)

    rng = np.random.default_rng(1)
    for size in range(1, len(kept) + 1):
        for _ in range(20):
            cols = rng.choice(len(kept), size=size, replace=False).tolist()
            total, live = scaled[:, cols].sum(axis=1), have[:, cols].any(axis=1)
            counted = live.sum()
            mean = total.sum() / counted
            spread = np.sqrt(max(total @ total / counted - mean * mean, 0.0))
            fast = float(mean / spread * np.sqrt(planner.TRADING_DAYS))
            slow = planner.sharpe(scaled, have, cols)
            assert abs(fast - slow) < 1e-9, f"moments disagree on {cols}: {fast} vs {slow}"

    for locked in ((), (0, 5)):
        for seq in planner.search(rho, scaled, have, locked=locked):
            cols = [*locked, *seq]
            # No ``own``, so the escape clause is inactive and nothing may reach the ceiling.
            # A one-Alpha portfolio has no pair at all, which reports as ``None``.
            collision, _pair, escaped = planner.worst_pair(rho, cols, locked=locked)
            assert not escaped, f"the escape clause fired without Sharpes: {cols}"
            assert (collision is None) == (len(cols) < 2), f"{cols} measured as {collision}"
            assert collision is None or collision < planner.CEILING, (
                f"search returned a portfolio over the ceiling: {cols} at {collision}"
            )
            # A repeated member would double-count one Alpha's PnL into the stream.
            assert len(set(cols)) == len(cols), f"duplicate member in {cols}"


def check_correlations() -> None:
    """Pairwise-complete correlation, checked against the masked form it replaced.

    ``correlations`` leans on ``z`` being exactly zero where an Alpha did not trade, so a pair's
    sums need only the *other* Alpha's mask and become dot products. That is only true for the
    sums it is applied to, and the fixture below is the one that can tell: every column is
    missing a different stretch, so no two share a calendar, and one pair overlaps under
    ``MIN_OVERLAP`` so the discard is exercised too.
    """
    rng = np.random.default_rng(7)
    days, alphas = 1200, 9
    m = rng.normal(size=(days, alphas))
    for col in range(alphas):
        start = int(rng.integers(0, days // 2))
        m[start : start + int(rng.integers(50, days // 3)), col] = np.nan
    m[: days - 100, 0] = np.nan  # leaves column 0 overlapping everything under MIN_OVERLAP

    have = ~np.isnan(m)
    z = np.nan_to_num(m)
    want = np.eye(alphas)
    for i in range(alphas - 1):
        both = have[:, i][:, None] & have[:, i + 1 :]
        count = both.sum(axis=0)
        safe = np.maximum(count, 1)
        left = np.where(both, z[:, i][:, None], 0.0)
        right = np.where(both, z[:, i + 1 :], 0.0)
        sum_l, sum_r = left.sum(0), right.sum(0)
        cov = (left * right).sum(0) - sum_l * sum_r / safe
        dev_l = np.sqrt(np.maximum((left**2).sum(0) - sum_l**2 / safe, 0.0))
        dev_r = np.sqrt(np.maximum((right**2).sum(0) - sum_r**2 / safe, 0.0))
        usable = (dev_l > 0) & (dev_r > 0) & (count >= planner.MIN_OVERLAP)
        rho = np.where(usable, cov / np.maximum(dev_l * dev_r, 1e-12), np.nan)
        want[i, i + 1 :] = rho
        want[i + 1 :, i] = rho

    got = planner.correlations(m)
    assert np.isnan(got).sum() > 0, "fixture exercises no thin pair, so it proves nothing"
    assert (np.isnan(got) == np.isnan(want)).all(), "a pair was discarded that should not be"
    np.testing.assert_allclose(got, want, atol=1e-12, equal_nan=True)


def check_flat_alpha() -> None:
    """An Alpha that never moves scores zero rather than dividing by its own zero spread.

    ``scale`` sends a zero-variance series to an all-zero column, which leaves the one-pass
    variance at exactly zero and the score at ``0/0``. The fixture above has no such Alpha, so
    this is the only thing covering that branch.
    """
    rng = np.random.default_rng(0)
    m = np.column_stack(
        [rng.normal(0.05, 1.0, 900), np.full(900, 3.0), np.zeros(900), rng.normal(0.05, 1.0, 900)]
    )
    m[:5, 3] = np.nan
    scaled, have = planner.scale(m), ~np.isnan(m)
    for flat in (1, 2):
        assert planner.sharpe(scaled, have, [flat]) == 0.0, f"column {flat} scored non-zero"

    with warnings.catch_warnings():  # an unsuppressed numpy warning is a failure
        warnings.simplefilter("error")
        found = planner.search(planner.correlations(m), scaled, have, depth=4)
    assert all(np.isfinite(planner.sharpe(scaled, have, list(seq))) for seq in found), found


def check_outside_submission(days: dict[str, dict[date, float]]) -> None:
    """An Alpha submitted from some other task still blocks the ones it collides with.

    BRAIN measures its ceiling against every Alpha on the account. The planner is handed one
    task's Alphas, so a submission made from a different task is not among them -- and until it
    was put into the matrix on its own account it was silently ignored, which is how a plan gets
    built against a permanent Alpha and then refused at submission.
    """
    outside = "b0c0"
    candidates = [a for a in sorted(days) if a != outside]

    blind = planner.plan(days, candidates)
    took_block = [r["alphaId"] for r in blind["order"] if r["alphaId"].startswith("b0")]
    assert took_block, "the fixture never picks block 0, so this proves nothing"

    found = planner.plan(days, candidates, locked_ids=[outside])
    assert found["locked"] == 1, f"the outside submission was dropped: {found['locked']}"
    members = [r["alphaId"] for r in found["order"]]
    assert outside in members, "the submission is part of the book and belongs in the order"
    assert [r for r in found["order"] if r["submitted"]], "it should be marked as submitted"
    # Its siblings correlate about 0.94 with it, so none of them may come along.
    siblings = [a for a in members if a.startswith("b0") and a != outside]
    assert not siblings, f"picked Alphas colliding with a submitted one: {siblings}"
    # And it is not counted as a candidate, nor allowed to answer "why not just submit one?".
    assert found["candidates"] == len(candidates), found["candidates"]
    kept, _dates, only = planner.grid(days, candidates)
    book, have = np.nan_to_num(only), ~np.isnan(only)
    best = max(planner.sharpe(book, have, [i]) for i in range(len(kept)))
    assert abs(found["bestSingle"] - round(best, 4)) < 1e-4, (found["bestSingle"], best)


def check_escape_clause() -> None:
    """The 10% rule frees an Alpha from a submitted one, and never from a fellow candidate.

    Both halves matter. Without it a mediocre submission blocks every stronger Alpha near it
    permanently; with it applied too widely the planner starts recommending pairs that move
    together, which is the whole thing it exists to avoid.
    """
    rng = np.random.default_rng(11)
    days = 2000
    base = rng.normal(loc=1.0 / np.sqrt(planner.TRADING_DAYS), size=days)
    # 0 is the weak incumbent; 1 is strong and collides with it; 2 and 3 are independent.
    columns = [
        base * 0.5,
        base * 0.5 + rng.normal(scale=0.2, size=days) + 0.09,
        rng.normal(loc=0.06, size=days),
        rng.normal(loc=0.06, size=days),
    ]
    m = np.column_stack(columns)
    have = ~np.isnan(m)
    scaled, book = planner.scale(m), np.nan_to_num(m)
    rho = planner.correlations(m)
    own = np.array([planner.sharpe(book, have, [i]) for i in range(m.shape[1])])

    assert rho[0, 1] >= planner.CEILING, f"fixture pair is not colliding: {rho[0, 1]:.3f}"
    assert own[1] >= planner.ESCAPE * own[0], f"fixture does not clear the 10%: {own}"

    blocked = planner.search(rho, scaled, have, locked=(0,), depth=3)
    assert all(1 not in seq for seq in blocked), "the incumbent did not block it without `own`"

    freed = planner.search(rho, scaled, have, locked=(0,), own=own, depth=3)
    assert any(1 in seq for seq in freed), "the 10% rule did not free it from the submission"

    # Same pair, neither submitted: the clause must not apply between two candidates.
    among = planner.search(rho, scaled, have, own=own, depth=3)
    for seq in among:
        assert not {0, 1} <= set(seq), f"the clause fired between two candidates: {seq}"

    # And the collision is reported with its provenance, so a legal 0.67 is not read as a bug.
    value, names, escaped = planner.worst_pair(rho, [0, 1], locked=(0,))
    assert value is not None and value >= planner.CEILING and escaped, (value, escaped)
    assert names == (0, 1), names
    _, _, unsubmitted = planner.worst_pair(rho, [0, 1])
    assert not unsubmitted, "a candidate-only collision was excused as an escape"

    # Nothing to measure is not zero correlation: one Alpha, and two that never overlap.
    assert planner.worst_pair(rho, [2]) == (None, None, False)
    apart = np.full((len(base), 2), np.nan)
    apart[: planner.MIN_OVERLAP, 0] = rng.normal(size=planner.MIN_OVERLAP)
    apart[-planner.MIN_OVERLAP :, 1] = rng.normal(size=planner.MIN_OVERLAP)
    assert planner.worst_pair(planner.correlations(apart), [0, 1]) == (None, None, False)


def check_two_books() -> None:
    """Every reported figure is the book the consultant will hold, not the one searched on.

    ``plan`` picks members on volatility-scaled series and reports on the plain sum. With one
    Alpha trading far louder than the rest the two books disagree, so a figure that slipped back
    onto ``scaled`` shows up here as a Sharpe that does not match its own equity curve.
    """
    rng = np.random.default_rng(3)
    days, loud = 3000, 40.0
    start = date(2014, 1, 1)
    calendar = [start + timedelta(days=i) for i in range(days)]
    drift = 1.0 / np.sqrt(planner.TRADING_DAYS)
    base = rng.normal(loc=drift, size=(days, 3))
    series = {}
    for block in range(3):
        for copy in range(2):
            size = loud if (block == 0 and copy == 0) else 1.0
            noise = base[:, block] + rng.normal(scale=0.25, size=days)
            series[f"b{block}c{copy}"] = dict(zip(calendar, (noise * size).tolist(), strict=True))

    found = planner.plan(series, sorted(series))
    assert found["order"], "the planner returned no portfolio"

    # The curve is the plain sum, so its Sharpe is the one reported beside it.
    curve = np.diff(np.asarray(found["curve"]), prepend=0.0)
    lived = float(curve.std())
    assert lived > 0, "a flat curve proves nothing"
    walked = float(curve.mean() / lived * np.sqrt(planner.TRADING_DAYS))
    assert abs(walked - found["sharpe"]) < 1e-3, (
        f"reported Sharpe {found['sharpe']} is not the curve's own {walked:.4f} -- "
        "a figure is being read off the scaled book"
    )

    # And a single Alpha scores the same on either book, so the comparison is commensurable.
    kept, _dates, grid = planner.grid(series, sorted(series))
    have = ~np.isnan(grid)
    for column in range(len(kept)):
        one = planner.sharpe(np.nan_to_num(grid), have, [column])
        assert abs(one - planner.sharpe(planner.scale(grid), have, [column])) < 1e-9, kept[column]


def check_leap_day() -> None:
    """Feb 29 stepped back onto a year without one. Unreachable at ``WINDOW_YEARS = 4`` until
    2104, but the constant is one edit away from making it reachable."""
    assert planner.to_earlier_year(date(2104, 2, 29), 4) == date(2100, 2, 28)
    assert planner.to_earlier_year(date(2024, 5, 17), 4) == date(2020, 5, 17)


def check_submittable() -> None:
    """An Alpha BRAIN never judged is not submittable. See ``vault.yields.is_submittable``."""
    assert submittable({}) is False, "an Alpha with no result read as submittable"
    assert submittable({"checks": []}) is False, "an Alpha with no checks read as submittable"
    assert submittable({"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}) is True
    assert submittable({"checks": [{"name": "LOW_SHARPE", "result": "FAIL"}]}) is False
    # PROD_CORRELATION is ignored here, so its failure must not refuse the Alpha.
    assert submittable({"checks": [{"name": "PROD_CORRELATION", "result": "FAIL"}]}) is True


def check_planner_candidates() -> None:
    """What reaches the planner is judged strictly, which is *not* what the Tasks screen shows.

    ``labs.study.submittable`` asks "has anything refused this yet", so a check still running
    reads as no refusal -- right for a list that fills in as BRAIN works. A submission is
    permanent, so ``api.tools._candidates`` asks the stricter question instead, and the two
    disagree on exactly the cases below. If they are ever unified, this is what says which way.
    """
    still_running = json.dumps(
        [
            {"name": "LOW_SHARPE", "result": "PASS"},
            {"name": "IS_LADDER_SHARPE", "result": "PENDING"},
        ]
    )
    assert submittable(json.loads(f'{{"checks": {still_running}}}')) is True, "premise changed"
    assert is_submittable(still_running) is False, "a PENDING check reached the planner"
    assert is_promising(still_running) is True, "a PENDING Alpha was not counted as pending"

    # A gating miss reports as WARNING straight after simulation and FAIL once settled, so
    # WARNING is a refusal here however inviting the word sounds.
    warned = json.dumps([{"name": "LOW_SHARPE", "result": "WARNING"}])
    assert is_submittable(warned) is False, "a WARNING check reached the planner"
    assert is_promising(warned) is False, "a warned Alpha was counted as merely pending"

    clean = json.dumps([{"name": "LOW_SHARPE", "result": "PASS"}])
    assert is_submittable(clean) is True, "a clean Alpha was refused"


def main() -> None:
    days = fixture()
    check_search(days)
    check_correlations()
    check_flat_alpha()
    check_outside_submission(days)
    check_escape_clause()
    check_two_books()
    check_leap_day()
    check_submittable()
    check_planner_candidates()
    found = planner.plan(days, sorted(days))

    assert found["order"], "the planner returned no portfolio"
    ids = [row["alphaId"] for row in found["order"]]

    # One Alpha per factor is all the independent risk on offer, so that is the size, and no
    # two members may come from the same block -- those correlate about 0.94, way over 0.5.
    #
    # Uniqueness, not coverage: counting distinct blocks passes on two members from each of
    # the three, which is exactly what the search returns when the ceiling stops binding.
    assert found["size"] == BLOCKS, f"expected {BLOCKS} Alphas, got {found['size']}"
    blocks = [i[:2] for i in ids]
    assert len(set(blocks)) == len(blocks), f"two Alphas from one block: {ids}"
    # Measured, and under the ceiling. ``None`` would mean no pair shared enough history, which
    # on a fixture where every Alpha trades every day would be a bug in the measurement.
    assert found["maxCorrelation"] is not None, "the fixture's correlations went unmeasured"
    assert found["maxCorrelation"] < planner.CEILING, (
        f"portfolio breaks the ceiling at {found['maxCorrelation']}"
    )
    assert not found["escapeUsed"], "nothing is submitted here, so no escape clause applies"
    assert len(found["worstPair"]) == 2, found["worstPair"]

    # Diversifying across three independent factors should lift Sharpe by roughly sqrt(3).
    lift = found["sharpe"] / found["bestSingle"]
    assert 1.4 < lift < 2.0, f"expected roughly sqrt(3) lift from diversifying, got {lift:.2f}"

    # The order shown is the order to submit in: strongest first.
    own = [row["sharpe"] for row in found["order"]]
    assert own == sorted(own, reverse=True), f"not ordered strongest first: {own}"

    # The curve is what the chart draws, so it has to line up with its own dates.
    assert len(found["curve"]) == len(found["dates"]) == found["days"]

    print(
        f"ok: {found['size']} Alphas, Sharpe {found['sharpe']:.2f} "
        f"({lift:.2f}x the best single), held out {found['heldOutSharpe']:.2f}, "
        f"max correlation {found['maxCorrelation']:.2f}"
    )


main()
