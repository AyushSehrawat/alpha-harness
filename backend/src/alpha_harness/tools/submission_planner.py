"""Submission Planner: which submittable Alphas to submit, and in what order.

BRAIN refuses a Power Pool Alpha whose PnL correlation against one already submitted reaches
0.5, and a submission is permanent. So the question is not "which Alpha is best" but which
*set* is best. The method, in full:

1. Line every Alpha's daily PnL up by date, and divide each series by its own volatility, so
   one Alpha cannot dominate the search just by trading larger. That is a search prior, not a
   book: every figure reported comes off the plain sum, which is what a submitted Alpha
   actually trades.
2. A portfolio is legal when every pair inside it correlates below 0.5.
3. Split history in four-fifths and one-fifth. On the first four-fifths, search for the legal
   portfolio with the best combined Sharpe at each size.
4. Combined Sharpe rises with size, peaks, then falls: past the peak each extra Alpha adds
   more redundancy than return. **The peak is the number to submit.**
5. Score that portfolio on the final fifth, which the search never saw. That is the honest
   estimate of what it will do next.
6. Re-run the search over the whole history and take that many Alphas. The split chose the
   size; the full history chooses the members.

Only step 4 is a decision, and it is made on the window the search can actually resolve --
one-fifth of ten years is about 500 days, over which the standard error of a Sharpe ratio is
about 0.7 (``sqrt((252 + S**2 / 2) / N)``, Lo 2002, confirmed by simulation). Adjacent sizes
differ by a tenth or two, so that window cannot rank them. The last fifth reports; it does not
choose.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Sequence

type Floats = npt.NDArray[np.float64]
type Bools = npt.NDArray[np.bool_]

#: Days two Alphas must share before their correlation means anything. Matches ``labs.ga``.
MIN_OVERLAP = 250
#: BRAIN's Power Pool ceiling. A pair at or above this is refused.
CEILING = 0.5
#: Past the ceiling, BRAIN still takes an Alpha whose Sharpe beats the one it collides with by
#: this much. Applied against submitted Alphas only -- see :func:`search`.
ESCAPE = 1.1
#: Correlation is measured over the last four calendar years (see :func:`window_start`). Measured:
#: against the full history this moves 451 of 38,503 pairs across the ceiling, so it is not a
#: detail -- 401 pairs the full history calls safe are ones BRAIN would refuse.
WINDOW_YEARS = 4
#: Share of history the size is chosen on. The rest is kept back to report against.
SPLIT = 0.8
#: Sequences kept between depths. Wide enough that the answer stopped moving at 278 Alphas.
BEAM = 60
#: Far past any peak seen in practice, so the search always runs through it and back down.
MAX_PICKS = 14

#: BRAIN annualises over 250 days, measured (see :mod:`..vault.metrics`).
TRADING_DAYS = 250


def grid(
    days: dict[str, dict[date, float]], alpha_ids: Sequence[str]
) -> tuple[list[str], list[date], Floats]:
    """The daily PnL of every Alpha on one shared calendar, ``NaN`` where it did not trade."""
    kept = [a for a in alpha_ids if days.get(a)]
    dates = sorted({d for a in kept for d in days[a]})
    at = {d: i for i, d in enumerate(dates)}
    m = np.full((len(dates), len(kept)), np.nan)
    for col, alpha_id in enumerate(kept):
        for day, value in days[alpha_id].items():
            m[at[day], col] = value
    return kept, dates, m


def correlations(m: Floats) -> Floats:
    """Pairwise Pearson correlation, each pair measured only on the days both Alphas traded.

    One masked matrix operation rather than ``labs.ga.correlation`` per pair: that is the same
    arithmetic, but 278 Alphas is 38,503 pairs and the loop costs half a minute.

    Every sum a pair needs is a Gram matrix, so all 38,503 pairs come out of five products at
    once. ``z`` is exactly zero where an Alpha did not trade, which is what lets a pair's sums
    carry only the *other* Alpha's mask; only the day count needs both. Measured 19x faster
    than the same arithmetic walked column by column, and identical to the last bit.
    """
    # Float: ``@`` on two boolean arrays is an OR-of-ANDs, which would turn ``count`` into
    # "did they ever overlap" and quietly retire the whole correlation ceiling.
    have = (~np.isnan(m)).astype(np.float64)
    z = np.nan_to_num(m)
    zz = z * z

    count = have.T @ have
    sum_l = z.T @ have
    sum_r = sum_l.T
    square_l = zz.T @ have
    square_r = square_l.T
    safe = np.maximum(count, 1.0)

    cov = (z.T @ z) - sum_l * sum_r / safe
    dev_l = np.sqrt(np.maximum(square_l - sum_l**2 / safe, 0.0))
    dev_r = np.sqrt(np.maximum(square_r - sum_r**2 / safe, 0.0))
    usable = (dev_l > 0) & (dev_r > 0) & (count >= MIN_OVERLAP)
    # Cancellation in the one-pass moments can land a correlation a few ulps outside [-1, 1],
    # which is harmless arithmetic and an alarming thing to print next to a 0.50 ceiling.
    out = np.clip(np.where(usable, cov / np.maximum(dev_l * dev_r, 1e-12), np.nan), -1.0, 1.0)
    np.fill_diagonal(out, 1.0)
    return out


def scale(m: Floats) -> Floats:
    """Each series divided by its own volatility, so every Alpha carries the same risk.

    Without this, equal weighting is equal *cash*, and the Alpha with the largest PnL swings
    takes over the portfolio: daily volatility spans a factor of twelve across one sweep.

    This is what the search picks on, never what ``plan`` reports. BRAIN trades a submitted
    Alpha at a fixed size, so this book cannot be held -- but selecting on it finds orthogonal
    Alphas rather than loud ones, and that orthogonality survives being flattened back to equal
    cash. See the note in :func:`plan` for the measurement.
    """
    sd = np.nanstd(m, axis=0)
    return np.nan_to_num(m) / np.where(sd > 0, sd, np.inf)


def sharpe(scaled: Floats, have: Bools, cols: Sequence[int]) -> float:
    """Annualised Sharpe of the combined stream, over the days any of its members traded.

    The members are summed rather than averaged: dividing by their count scales the stream by
    a constant, and a Sharpe ratio does not notice a constant.
    """
    if not len(cols):
        return 0.0
    stream = scaled[:, cols].sum(axis=1)[have[:, cols].any(axis=1)]
    spread = float(stream.std())
    if spread == 0 or not np.isfinite(spread):
        return 0.0
    return float(stream.mean() / spread * np.sqrt(TRADING_DAYS))


def search(
    rho: Floats,
    scaled: Floats,
    have: Bools,
    *,
    locked: tuple[int, ...] = (),
    own: Floats | None = None,
    ceiling: float = CEILING,
    beam: int = BEAM,
    depth: int = MAX_PICKS,
) -> list[tuple[int, ...]]:
    """The best legal portfolio found at each size, by beam search.

    Picking the highest-Sharpe Alpha first and taking whatever still fits is the obvious
    alternative, but the strongest member of a cluster locks out every weaker sibling, and
    that choice cannot be undone. Carrying several part-built portfolios recovers most of what
    that throws away.

    What it costs is the catch: this depth and beam over 278 Alphas asks for about 200,000
    candidate scores. So a candidate is never scored on its own -- each frontier entry scores
    every nominee at once, off the running stream it already holds, which measured 3-12x faster
    than one at a time for identical portfolios. ``plan`` runs two searches, in a few seconds.
    """
    n = rho.shape[0]
    # The diagonal is 1.0, so an Alpha is never legal against itself and cannot be re-picked.
    under = np.isnan(rho) | (rho < ceiling)

    # BRAIN admits a correlated Alpha that beats the one it collides with by 10%
    # (``getting-started-power-pool-alphas.md:20``), and that clause is taken against already
    # submitted Alphas only. Between two candidates it is refused: both are still free to drop,
    # so keeping a redundant pair is a choice, and the point of the tool is not to make it.
    # Against a submission it is not a choice -- that Alpha is permanent, and without the clause
    # a mediocre incumbent blocks every stronger Alpha near it for good. Measured on this vault:
    # one submission at Sharpe 2.45 was blocking 124 candidates, 3 of which BRAIN would take.
    #
    # Only a permission, never a push: the beam still ranks on combined Sharpe, so a correlated
    # Alpha is picked up only where it earns its place anyway.
    if locked:
        rows = under[list(locked)]
        if own is not None:
            theirs = own[list(locked)][:, None]
            # A Sharpe at or below zero would make ``ESCAPE * theirs`` easier to clear the worse
            # the incumbent is, which would free the whole pool rather than the Alphas that beat
            # it. Nothing escapes a submission that is not itself making money.
            rows = rows | ((own[None, :] >= ESCAPE * theirs) & (theirs > 0))
        legal_against_locked = rows.all(axis=0)
    else:
        legal_against_locked = np.ones(n, dtype=bool)

    # Per-Alpha moments, so a nominee's contribution to the combined stream is a lookup.
    column_sum = scaled.sum(axis=0)
    column_square = (scaled * scaled).sum(axis=0)
    traded = have.astype(np.float64)

    def stream_of(cols: list[int]) -> tuple[Floats, Bools]:
        """The combined PnL of ``cols`` and the days any of them traded."""
        if not cols:
            return np.zeros(scaled.shape[0]), np.zeros(scaled.shape[0], dtype=bool)
        return scaled[:, cols].sum(axis=1), have[:, cols].any(axis=1)

    frontier: list[tuple[tuple[int, ...], Floats, Bools]] = [((), *stream_of(list(locked)))]
    best: list[tuple[int, ...]] = []
    for _ in range(depth):
        scored: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {}
        for seq, total, live in frontier:
            # Submissions carry the escape clause, candidates picked along the way do not.
            eligible = np.flatnonzero(
                legal_against_locked & under[list(seq)].all(axis=0) if seq else legal_against_locked
            )

            # Every nominee's Sharpe in one pass. ``scaled`` is zero where an Alpha did not
            # trade, so a day outside the mask adds nothing and the sums below need no
            # masking; only the day *count* does. Deliberately a one-pass variance, which
            # trades a little precision for the speed -- it only ranks candidates here, and
            # every figure the user is shown comes from ``sharpe`` in ``plan``.
            days = live.sum() + (~live).astype(np.float64) @ traded
            totals = total.sum() + column_sum
            squares = total @ total + 2 * (total @ scaled) + column_square
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = totals / days
                spread = np.sqrt(np.maximum(squares / days - mean * mean, 0.0))
                scores = mean / spread * np.sqrt(TRADING_DAYS)

            for nominee in eligible.tolist():
                key = tuple(sorted((*seq, nominee)))
                if key in scored:
                    continue
                score = float(scores[nominee])
                scored[key] = (score if np.isfinite(score) else 0.0, (*seq, nominee))
        if not scored:
            break
        ranked = sorted(scored.values(), key=lambda pair: -pair[0])[:beam]
        # Rebuilt for the survivors only: holding a stream per candidate would cost a
        # gigabyte at this beam width.
        frontier = [(seq, *stream_of([*locked, *seq])) for _, seq in ranked]
        best.append(ranked[0][1])
    return best


def worst_pair(
    rho: Floats, cols: Sequence[int], locked: tuple[int, ...] = ()
) -> tuple[float | None, tuple[int, int] | None, bool]:
    """The largest correlation inside a portfolio, who it is between, and whether BRAIN's 10%
    rule is what admits it.

    ``None`` rather than ``0.0`` when there is no pair to measure -- a single Alpha, or none
    sharing ``MIN_OVERLAP`` days. Those are "not measurable", and reporting them as zero says
    "perfectly uncorrelated", which is the most reassuring thing a failed measurement could
    possibly claim.
    """
    pairs = [
        (float(rho[a, b]), (a, b))
        for i, a in enumerate(cols)
        for b in cols[i + 1 :]
        if not np.isnan(rho[a, b])
    ]
    if not pairs:
        return None, None, False
    value, pair = max(pairs)
    # Over the ceiling is only legal against a submission, so a collision that involves one is
    # the escape clause doing its job rather than a portfolio BRAIN would refuse.
    return value, pair, value >= CEILING and bool({*pair} & set(locked))


def plan(
    days: dict[str, dict[date, float]],
    alpha_ids: Sequence[str],
    *,
    locked_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """The Alphas to submit and the order to submit them in. See this module's docstring.

    ``locked_ids`` need not appear in ``alpha_ids``. A submission made from another task, or
    from the Pool, still constrains everything here -- BRAIN measures the ceiling against every
    Alpha on the account, not against the ones this run happened to look at -- so it goes into
    the matrix on its own account.
    """
    everything = list(dict.fromkeys([*alpha_ids, *locked_ids]))
    submitted = set(locked_ids)
    kept, dates, full = grid(days, everything)
    empty: dict[str, Any] = {
        "candidates": len([a for a in kept if a not in submitted]),
        "locked": 0,
        # Locked Alphas count here too, and matter more: one with no stored PnL is a ceiling
        # this cannot enforce, which is how a plan gets refused at submission.
        "missing": [a for a in everything if a not in set(kept)],
        "size": 0,
        "order": [],
        "sizes": [],
        "trainSharpe": 0.0,
        "heldOutSharpe": 0.0,
        "sharpe": 0.0,
        "bestSingle": 0.0,
        "maxCorrelation": None,
        "worstPair": [],
        "escapeUsed": False,
        "days": 0,
        "curve": [],
        "dates": [],
    }
    if len(kept) < 2:
        empty["reason"] = "too_few"
        return empty

    index = {alpha_id: i for i, alpha_id in enumerate(kept)}
    locked = tuple(index[a] for a in locked_ids if a in index)
    candidates = [index[a] for a in alpha_ids if a in index and a not in submitted]

    # The ceiling is the platform's, so it is measured the platform's way: four calendar years,
    # whatever the objective below is computed on.
    gate = bisect_left(dates, window_start(dates[-1]))
    rho = correlations(full[gate:])

    # Two books. Every choice is made on ``scaled``; every number reported comes off ``book``.
    #
    # A submitted Alpha trades at a fixed size, so the combination the consultant will actually
    # hold is the plain sum -- ``scaled`` describes a risk-parity book nobody can hold, and
    # reporting it overstates the result. Measured on 338 vault Alphas: 8.82 against the 7.38
    # they would get. But choosing on the plain sum is worse, not better. Daily volatility spans
    # 25x here, so the loudest Alpha owns the variance, adding a quieter one looks useless, and
    # the search collapses to four members: across five splits that cost 0.34 to 1.45 Sharpe
    # out of sample. Dividing by volatility is a bad description of the book and a good prior
    # for the search, so it is kept for one job and dropped for the other.
    scaled, have = scale(full), ~np.isnan(full)
    book = np.nan_to_num(full)
    cut = int(len(dates) * SPLIT)
    if cut < MIN_OVERLAP or len(dates) - cut < MIN_OVERLAP:
        # Both halves have to be worth measuring, so a four-fifths split needs 1250 days. Said
        # out loud rather than returned as an empty plan: "no history yet" and "no Alpha worth
        # submitting" look identical on the screen and need different answers.
        empty["reason"] = "short_history"
        empty["days"] = len(dates)
        return empty

    # Step 1: the size. Combined Sharpe over the training window peaks and then falls away,
    # and that peak is the only thing the split is asked to decide.
    #
    # On its own correlations, not the platform's. ``rho`` runs four years back from today, so
    # over ten years of history it covers the held-out fifth -- letting it gate this search
    # would decide the size from pairs that had not happened yet. The step that picks members
    # keeps ``rho``, because there the question is what BRAIN will accept today.
    train_gate = bisect_left(dates[:cut], window_start(dates[cut - 1]))
    rho_train = correlations(full[train_gate:cut])
    own_train = np.array([sharpe(book[:cut], have[:cut], [i]) for i in range(len(kept))])
    # Scaled on the training rows alone: whole-history volatility lets the held-out fifth
    # steer the search.
    train_scaled = scale(full[:cut])
    trained = search(rho_train, train_scaled, have[:cut], locked=locked, own=own_train)
    if not trained:
        # Everything left collides with something already submitted.
        empty["reason"] = "nothing_legal"
        return empty
    ranking = [sharpe(train_scaled, have[:cut], locked + seq) for seq in trained]
    size = int(np.argmax(ranking)) + 1
    sizes = [round(sharpe(book[:cut], have[:cut], locked + seq), 4) for seq in trained]

    # Step 2: what that portfolio did on the fifth of history the search never saw.
    checked = list(locked + trained[size - 1])
    held_out = sharpe(book[cut:], have[cut:], checked)

    # Step 3: the members, chosen over everything now known.
    own_all = np.array([sharpe(book, have, [i]) for i in range(len(kept))])
    orders = search(rho, scaled, have, locked=locked, own=own_all, depth=size)
    if not orders:
        # Legal over the training years, but colliding with a submission over the last four.
        empty["reason"] = "nothing_legal"
        return empty
    chosen = list(locked + orders[-1])
    # A Sharpe ratio does not notice a constant, so one Alpha scores the same on either book.
    alone = [float(own_all[i]) for i in chosen]
    order = sorted(zip(chosen, alone, strict=True), key=lambda pair: -pair[1])
    # Against every candidate, not just the chosen: the question this answers is "why not just
    # submit the single best Alpha and stop?". Already-submitted Alphas are not an answer to it.
    best_single = float(max((own_all[i] for i in candidates), default=float(own_all.max())))

    collision, pair, escaped = worst_pair(rho, chosen, locked=locked)
    live = have[:, chosen].any(axis=1)
    stream = book[:, chosen].sum(axis=1)[live]
    return {
        "candidates": len(candidates),
        "locked": len(locked),
        "missing": [a for a in everything if a not in index],
        "size": len(chosen),
        "order": [
            {"alphaId": kept[i], "sharpe": round(own, 4), "submitted": i in locked}
            for i, own in order
        ],
        #: What the split measured: Sharpe at each size on the training window, and the
        #: unseen-fifth score of the size it chose.
        "sizes": sizes,
        "trainSharpe": round(sizes[size - 1], 4),
        "heldOutSharpe": round(held_out, 4),
        "sharpe": round(sharpe(book, have, chosen), 4),
        "bestSingle": round(best_single, 4),
        "maxCorrelation": None if collision is None else round(collision, 4),
        "worstPair": [] if pair is None else [kept[pair[0]], kept[pair[1]]],
        "escapeUsed": escaped,
        "days": int(live.sum()),
        "curve": [round(float(v), 4) for v in np.cumsum(stream)],
        "dates": [dates[int(i)].isoformat() for i in np.flatnonzero(live)],
    }


def window_start(last: date) -> date:
    """First day BRAIN correlates over: 1 January, ``WINDOW_YEARS`` calendar years back including
    ``last``'s own. Whole years, not four years back from ``last``: measured, this reproduces
    BRAIN's Power Pool correlations to four decimals on all eight pairs checked, and the rolling
    window was off by up to 0.0005."""
    return date(last.year - WINDOW_YEARS + 1, 1, 1)
