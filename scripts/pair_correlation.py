"""Pairwise daily-PnL correlation across a set of candidate Alphas, before submitting any.

``SELF_CORRELATION`` answers one Alpha at a time, and only against what is *already*
submitted. That leaves the question a day's shortlist actually poses -- will these four
collide with each other once the first one goes in -- unanswered until it is too late to
choose differently. The vault already stores each Alpha's daily series, so the whole matrix
costs nothing and no BRAIN call.

    python scripts/pair_correlation.py Vkajqdj8 N1aM9mPE 78NO9d18 RRbX0q5b
    python scripts/pair_correlation.py --limit 0.7 <ids...>

The harness must be running; it reads ``GET /api/vault/alphas/{id}/detail``, which downloads
the daily PnL the first time it is asked and caches it afterwards.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
#: BRAIN's self-correlation cutoff, for marking the matrix.
LIMIT = 0.7
#: Below this many shared trading days a correlation is not worth printing.
MIN_DAYS = 60


def daily(alpha_id: str) -> dict[str, float] | None:
    """The Alpha's daily PnL by date, differenced back from the stored cumulative series."""
    request = urllib.request.Request(
        f"{BASE}/api/vault/alphas/{alpha_id}/detail",
        headers={"content-type": "application/json"},
    )
    try:
        body = json.load(urllib.request.urlopen(request, timeout=180))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  {alpha_id}: unread ({type(exc).__name__})")
        return None
    pnl, dates = body.get("pnl") or [], body.get("dates") or []
    if len(pnl) < 2:
        print(f"  {alpha_id}: no stored PnL ({body.get('problem') or 'empty series'})")
        return None
    print(f"  {alpha_id}: {len(pnl) - 1} days, {dates[0]} to {dates[-1]}")
    return {dates[i]: pnl[i] - pnl[i - 1] for i in range(1, len(pnl))}


def correlation(x: dict[str, float], y: dict[str, float]) -> tuple[float | None, int]:
    shared = sorted(set(x) & set(y))
    if len(shared) < MIN_DAYS:
        return None, len(shared)
    a = [x[d] for d in shared]
    b = [y[d] for d in shared]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    dev_a = sum((v - mean_a) ** 2 for v in a) ** 0.5
    dev_b = sum((v - mean_b) ** 2 for v in b) ** 0.5
    if not dev_a or not dev_b:
        return None, len(shared)
    top = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(len(a)))
    return top / (dev_a * dev_b), len(shared)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alpha_ids", nargs="+")
    parser.add_argument("--limit", type=float, default=LIMIT)
    args = parser.parse_args()

    print("daily PnL:")
    series = {a: s for a in dict.fromkeys(args.alpha_ids) if (s := daily(a)) is not None}
    if len(series) < 2:
        raise SystemExit("\nNeed at least two Alphas with a stored series.")

    found = list(series)
    width = max(len(a) for a in found)
    print(f"\npairwise correlation (limit {args.limit}):\n")
    print(" " * (width + 2) + "  ".join(f"{a:>{width}}" for a in found))
    worst = (0.0, "", "")
    for row in found:
        cells = []
        for column in found:
            if row == column:
                cells.append(f"{'--':>{width}}")
                continue
            value, _shared = correlation(series[row], series[column])
            cells.append(f"{'n/a' if value is None else f'{value:+.3f}':>{width}}")
            if value is not None and abs(value) > abs(worst[0]):
                worst = (value, row, column)
        print(f"{row:>{width}}  " + "  ".join(cells))

    print()
    if abs(worst[0]) >= args.limit:
        print(f"OVER THE LIMIT: {worst[1]} vs {worst[2]} at {worst[0]:+.3f}. Submit one, not both.")
    else:
        print(f"Worst pair {worst[1]} vs {worst[2]} at {worst[0]:+.3f} - all clear of {args.limit}.")
        print("Note this measures past co-movement only: Alphas built on one signal can still")
        print("decay together even when their historical correlation is near zero.")


if __name__ == "__main__":
    main()
