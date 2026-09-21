"""Rank a sweep's own trials by after-cost margin, straight out of the local `trial` table.

Every lab simulation stores BRAIN's full stats and check payload in `trial.result`, so a
finished sweep can be screened with no BRAIN call, no quota and no rate limit. `collect.py`
in `.cache/stages/` re-fetched each Alpha over the API, which 429s and takes minutes.

    python scripts/sweep_margin.py 95 96 97              # one or more lab task ids
    python scripts/sweep_margin.py 95 --regular          # the Regular bar, not Power Pool
    python scripts/sweep_margin.py 95 --min-bps 15 --top 15

The gate is **inverted**, as in `collect.py`: an Alpha is clean when the set of FAILs left
after subtracting the checks the track does not score is empty -- so a check name never seen
before counts against it. `WARNING` and `PENDING` are reported as unresolved, never as a pass;
only `GET /api/alphas/{id}/check` settles a correlation.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3

# Outside the repo: the packaged app and this checkout share one store.
DATA = os.environ.get("AH_DATA_DIR") or os.path.expanduser("~/.alpha-harness")
DB = os.path.join(DATA, "harness.db")
#: Checks Power Pool does not score (`kb/platform/submission-tracks.md`).
NOT_PP = {"LOW_SHARPE", "LOW_FITNESS", "LOW_2Y_SHARPE", "IS_LADDER_SHARPE",
          "CONCENTRATED_WEIGHT", "PROD_CORRELATION", "LOW_RETURNS", "HIGH_DRAWDOWN",
          "LOW_MARGIN"}
INFORMATIONAL = {"MATCHES_COMPETITION", "MATCHES_THEMES", "MATCHES_PYRAMID",
                 "OSMOSIS_ALLOCATION", "DATA_DIVERSITY", "REGULAR_SUBMISSION",
                 "D0_SUBMISSION", "CLUSTER_TEST", "ALPHA_PYRAMID", "UNITS",
                 "POWER_POOL_DESCRIPTION_FORMAT", "POWER_POOL_DESCRIPTION_LENGTH"}
#: Every Power Pool submission is scored on these, so an unresolved one is a real unknown.
#: CONCENTRATED_WEIGHT is here because a WARNING on it carries the value and the limit, and a
#: value over the limit is what the live check turns into a FAIL.
UNRESOLVED_MATTERS = {"LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
                      "SELF_CORRELATION", "POWER_POOL_CORRELATION", "CONCENTRATED_WEIGHT"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("study_ids", nargs="+", type=int)
    parser.add_argument("--min-bps", type=float, default=8.0)
    parser.add_argument("--min-sharpe", type=float, default=1.0, help="the Power Pool bar")
    parser.add_argument("--max-turnover", type=float, default=0.70)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--regular", action="store_true", help="score the Regular bar instead")
    parser.add_argument("--all", action="store_true", help="also print rows that failed a check")
    args = parser.parse_args()

    ignore = INFORMATIONAL if args.regular else (NOT_PP | INFORMATIONAL)
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row

    for study_id in args.study_ids:
        rows = connection.execute(
            "select expression, settings, result, alpha_id from trial "
            "where study_id = ? and result is not null", (study_id,)).fetchall()
        census: collections.Counter = collections.Counter()
        kept = []
        for row in rows:
            result = json.loads(row["result"])
            stats, checks = result.get("stats") or {}, result.get("checks") or []
            if not stats or not row["alpha_id"]:
                continue
            failed = {c["name"] for c in checks if c["result"] == "FAIL"} - ignore
            unresolved = {c["name"] for c in checks
                          if c["result"] in ("WARNING", "PENDING")} & UNRESOLVED_MATTERS
            census.update(c["name"] for c in checks if c["result"] == "FAIL")
            bps = (stats.get("margin") or 0) * 10_000
            # A field with no history before the Test Period annualises a few weeks of PnL into a
            # huge margin. Only a live check reports LOW_DURATION; an empty TRAIN block is the
            # same fact, free and local.
            if stats.get("train_sharpe") == 0 and stats.get("sharpe"):
                failed.add("SHORT_HISTORY(train_sharpe=0)")
            sub = next((c for c in checks if c["name"] == "LOW_SUB_UNIVERSE_SHARPE"), None)
            over = ""
            if sub and sub.get("limit") and sub.get("value") is not None:
                over = f"{(sub['value'] / sub['limit'] - 1) * 100:+.0f}%"
            if not args.all and (failed or bps < args.min_bps
                                 or (stats.get("sharpe") or 0) < args.min_sharpe
                                 or not 0.01 <= (stats.get("turnover") or 0) <= args.max_turnover):
                continue
            kept.append({
                "id": row["alpha_id"], "bps": bps, "sub": over, "failed": sorted(failed),
                "unresolved": sorted(unresolved), "expression": row["expression"],
                "settings": json.loads(row["settings"]), **stats,
            })

        kept.sort(key=lambda r: -r["bps"])
        print(f"\n=== study {study_id}: {len(rows)} trials, {len(kept)} clean at or above "
              f"{args.min_bps:g} bps ===")
        if census:
            print("  FAIL census:", ", ".join(f"{n}={c}" for n, c in census.most_common(8)))
        print(f"  {'id':10} {'shrp':>5} {'2Ytr':>5} {'tvr':>6} {'ret':>6} {'margin':>8} "
              f"{'sub':>5}  {'universe':9} {'neutral':13} expression")
        for row in kept[:args.top]:
            settings = row["settings"]
            print(f"  {row['id']:10} {row.get('sharpe', 0):5.2f} {row.get('train_sharpe', 0):5.2f} "
                  f"{row.get('turnover', 0):6.3f} {row.get('returns', 0):6.3f} {row['bps']:7.1f}b "
                  f"{row['sub']:>5}  {settings.get('universe', ''):9} "
                  f"{settings.get('neutralization', ''):13} {row['expression'][:72]}")
            if row["unresolved"]:
                print(f"{'':12} unresolved: {', '.join(row['unresolved'])}")
            if row["failed"]:
                print(f"{'':12} FAILED: {', '.join(row['failed'])}")


if __name__ == "__main__":
    main()
