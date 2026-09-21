"""Rank the vault by after-cost margin, which is what a submitted Alpha is actually paid on.

`export_vault.py` screens on each market's Sharpe bar. That is the submission gate, not the
economics: BRAIN's combined pool is charged a trading cost, and an Alpha whose margin is under
that cost contributes nothing once it joins. Margin is already in every vault row, so this
screen costs no quota and no BRAIN call.

    margin_bps = returns / (2 * 252 * turnover) * 10_000        (= vault `margin` * 10_000)
    net at C   = margin_bps - C

    python scripts/margin_screen.py                        every non-USA scope, >= 12 bps
    python scripts/margin_screen.py --min-bps 8 --top 20
    python scripts/margin_screen.py --scope DEU:0 --scope GBR:0
    python scripts/margin_screen.py --min-bps 14 --effect  # + BRAIN's Effect-on-pool per row

A book's margin is the turnover-weighted blend of its members, not the mean of their margins:

    book margin_bps = sum(returns) / (2 * 252 * sum(turnover)) * 10_000

so `--book` reports what the submitted non-USA pool earns and where this shortlist would move it.
Rows here are a shortlist, never a verdict - stored payloads leave correlation checks PENDING.
Resolve with `GET /api/alphas/{id}/check`, which also spends one of BRAIN's hourly correlation
requests, so resolve a named few rather than a page.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT = os.path.join(ROOT, ".cache", "vault")
BASE = "http://localhost:8000"
#: Charged on every dollar traded; the Portfolio page defaults to the same number.
COST_BPS = 5.0
#: The pre-scored composites every consultant book already holds. They read production
#: correlation 0.78-0.91 whatever the wrapper, so they are noise in a shortlist.
CROWDED = re.compile(r"mdl216|arm[a-z]*(score|rank)|rel_val_|star_|score_delta|_percentile_")
METRICS = ("sharpe", "fitness", "turnover", "returns", "margin")


def rows(scopes: set[str], include_usa: bool) -> list[dict]:
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(VAULT, "*.csv"))):
        region, _, delay = os.path.basename(path)[:-4].partition("-d")
        scope = f"{region}:{delay}"
        if scopes and scope not in scopes:
            continue
        if not include_usa and region == "USA":
            continue
        for row in csv.DictReader(open(path, newline="", encoding="utf-8")):
            if not row.get("margin") or not row.get("sharpe"):
                continue
            try:
                for key in METRICS:
                    row[key] = float(row[key] or 0)
            except ValueError:
                continue
            row["scope"], row["bps"] = scope, row["margin"] * 10_000
            out.append(row)
    return out


def members() -> list[dict]:
    """Every submitted Alpha the harness knows about, from the Portfolio page."""
    try:
        return json.load(
            urllib.request.urlopen(f"{BASE}/api/portfolio/members", timeout=60))["members"]
    except OSError as exc:
        print(f"  (portfolio unread: {exc})", file=sys.stderr)
        return []


def book(pool: list[dict], exclude: set[str]) -> tuple[float, float]:
    """Total returns and turnover of the submitted non-USA pool."""
    live = [m for m in pool
            if m["region"] != "USA" and m["returns"] is not None and m["alphaId"] not in exclude]
    return sum(m["returns"] for m in live), sum(m["turnover"] for m in live)


def effect(alpha_id: str) -> str:
    """BRAIN's own before/after on the partition this Alpha would join."""
    try:
        body = json.load(urllib.request.urlopen(
            f"{BASE}/api/alphas/{alpha_id}/performance", timeout=300))
    except OSError as exc:
        return f"unread ({type(exc).__name__})"
    before, after = body["stats"].get("before"), body["stats"].get("after")
    if not before:
        return f"{body['partitionName']}: first in partition, nothing to dilute"
    return (f"{body['partitionName']}: sharpe {after['sharpe'] - before['sharpe']:+.2f} "
            f"fitness {after['fitness'] - before['fitness']:+.2f} "
            f"margin {(after['margin'] - before['margin']) * 10_000:+.2f}b "
            f"drawdown {after['drawdown'] - before['drawdown']:+.4f}")


def rate(value: float, cost: float) -> str:
    return f"{value:5.2f} bps  net {value - cost:+.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-bps", type=float, default=12.0)
    ap.add_argument("--max-turnover", type=float, default=1.0)
    ap.add_argument("--min-sharpe", type=float, default=1.0, help="the Power Pool bar")
    ap.add_argument("--scope", action="append", default=[], help="REGION:DELAY, repeatable")
    ap.add_argument("--top", type=int, default=12, help="rows per scope")
    ap.add_argument("--cost", type=float, default=COST_BPS)
    ap.add_argument("--include-usa", action="store_true")
    ap.add_argument("--keep-crowded", action="store_true",
                    help="keep pre-scored composites, which normally fail prod correlation")
    ap.add_argument("--effect", action="store_true",
                    help="ask BRAIN for Effect-on-pool per surviving row (slow, paced)")
    ap.add_argument("--book", action="store_true", help="what the shortlist does to the pool")
    ap.add_argument("--exclude", action="append", default=[], help="alpha id, repeatable")
    args = ap.parse_args()

    # A submitted Alpha is not a candidate, and it is already inside the book figure below.
    pool = members()
    skip = set(args.exclude) | {m["alphaId"] for m in pool}
    found = [r for r in rows(set(args.scope), args.include_usa)
             if r["pp_ok"] and not r["suspect"] and r["alpha_id"] not in skip
             and r["sharpe"] >= args.min_sharpe and r["turnover"] <= args.max_turnover
             and r["bps"] >= args.min_bps
             and (args.keep_crowded or not CROWDED.search(r["expression"].lower()))]
    if not found:
        print("nothing clears that screen.")
        return

    by_scope: dict[str, list[dict]] = {}
    for row in found:
        by_scope.setdefault(row["scope"], []).append(row)

    picks: list[dict] = []
    for scope in sorted(by_scope, key=lambda s: -max(r["bps"] for r in by_scope[s])):
        kept = sorted(by_scope[scope], key=lambda r: -r["bps"])[:args.top]
        picks += kept
        print(f"\n=== {scope}: {len(by_scope[scope])} rows at or above {args.min_bps:g} bps ===")
        print(f"{'id':10} {'shrp':>5} {'fit':>5} {'tvr':>6} {'ret':>6} {'margin':>8} "
              f"{'net':>6} {'neutralization':14} {'dcy':>3}  expression")
        for r in kept:
            print(f"{r['alpha_id']:10} {r['sharpe']:5.2f} {r['fitness']:5.2f} {r['turnover']:6.3f} "
                  f"{r['returns']:6.3f} {r['bps']:7.1f}b {r['bps'] - args.cost:+6.1f} "
                  f"{r['neutralization'][:14]:14} {r['decay']:>3}  {r['expression'][:78]}")
            if args.effect:
                print(f"           -> {effect(r['alpha_id'])}")
                time.sleep(2)

    if not args.book or not pool:
        return
    have_returns, have_turnover = book(pool, {r["alpha_id"] for r in picks})
    add = sorted(picks, key=lambda r: -r["bps"])[:4]
    with_returns = have_returns + sum(r["returns"] for r in add)
    with_turnover = have_turnover + sum(r["turnover"] for r in add)
    margin = lambda ret, tvr: ret / (2 * 252 * tvr) * 10_000  # noqa: E731
    print(f"\nnon-USA pool now:              {rate(margin(have_returns, have_turnover), args.cost)}")
    print(f"with the top 4 of this screen: {rate(margin(with_returns, with_turnover), args.cost)}"
          f"   ({', '.join(r['alpha_id'] for r in add)})")


if __name__ == "__main__":
    main()
