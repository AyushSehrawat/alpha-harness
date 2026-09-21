"""Export the simulated vault to one CSV per region/delay.

Replaces `.cache/export.py` + `.cache/shortlist.py`, which covered delay 0 only, carried no
region column, and screened everything against DEU d0's sharpe 2.69 / fitness 1.50. The bar is
per market, and BRAIN returns it attached to every check — so nothing here is hardcoded.

    python scripts/export_vault.py                     every scope -> .cache/vault/
    python scripts/export_vault.py --scope DEU:0       one scope
    python scripts/export_vault.py --over-bar          only rows clearing their own market's bar
    python scripts/export_vault.py --absorb-legacy .cache/alphas_d0.json

Metrics come from `trial.result`, which carries BRAIN's own stats and checks with their limits.
That covers the optimizer path only, so `--absorb-legacy` distils the pre-lab export down to the
alphas it is still the only local copy of and keeps them in `_legacy_metrics.json`.

A row is only as good as its source: correlation checks are PENDING in every stored payload, and
`LOW_DURATION` / `LOW_COVERAGE` never appear at all. Treat `over_bar` and `pp_ok` as a shortlist,
never a verdict — resolve with `GET /api/alphas/{id}/check`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Outside the repo: the packaged app and this checkout share one store.
DATA = os.environ.get("AH_DATA_DIR") or os.path.expanduser("~/.alpha-harness")
DB = os.path.join(DATA, "harness.db")
OUT = os.path.join(ROOT, ".cache", "vault")
LEGACY = "_legacy_metrics.json"

# Free under the Power Pool field cap.
GROUPING = {"country", "industry", "subindustry", "currency", "market", "sector", "exchange",
            "split", "adjfactor"}
# Not data fields either: named-argument keywords and bare literals.
NOT_A_FIELD = {"true", "false", "nan", "std", "d", "n", "k", "lambda_min", "lambda_max",
               "target_tvr", "hump", "filter", "driver", "sigma", "max", "min"}

INFIX = re.compile(r"[+\-*/^]|[<>]=?|[!=]=|&&|\|\||\?|:")
CALL = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
IDENT = re.compile(r"\b([a-zA-Z_]\w*)\b(?!\s*\()")

COLS = ["alpha_id", "sharpe", "fitness", "turnover", "returns", "drawdown", "margin",
        "universe", "neutralization", "decay", "truncation", "ops", "fields",
        "over_bar", "pp_ok", "suspect", "failed", "expression"]

# Identifiers, dates and metadata score spectacularly because they barely move. Five reached a
# shortlist before this became a rule — see .cache/kb/method/field-screening.md.
ARTIFACT_HINTS = ("unit_name", "ticker", "companyname", "_name", "splitfactor", "split_factor",
                  "_iso", "_cusip", "sedol", "isin", "unreliable_user", "bad_user",
                  "record_count", "_entitlement", "_version", "_length", "analyststart",
                  "flag_name", "prior_close_price", "annual_price_peak")


def suspect(stats: dict, expr: str) -> str:
    """Flag the artifact signature: a book that never draws down, or huge returns on almost no
    trading. Neither is a signal, and both clear the metric checks handsomely."""
    sharpe, dd = stats.get("sharpe"), stats.get("drawdown")
    if sharpe and sharpe > 0 and dd == 0:
        return "Y"
    ret, tvr = stats.get("returns"), stats.get("turnover")
    if ret is not None and tvr is not None and ret > 0.25 and tvr < 0.10:
        return "Y"
    low = (expr or "").lower()
    return "Y" if any(h in low for h in ARTIFACT_HINTS) else ""

# Checks Power Pool does not score, so failing one says nothing about PP eligibility.
PP_IGNORES = {"LOW_SHARPE", "LOW_FITNESS", "LOW_2Y_SHARPE", "IS_LADDER_SHARPE", "LOW_RETURNS",
              "CLUSTER_TEST", "DATA_DIVERSITY", "MATCHES_PYRAMID", "MATCHES_COMPETITION",
              "OSMOSIS_ALLOCATION", "UNITS", "REGULAR_SUBMISSION", "D0_SUBMISSION"}


def failing(name: str, result: str, value, limit) -> bool:
    """A WARNING carrying no numbers means *not computed*, not a refusal — `MATCHES_COMPETITION`
    and `OSMOSIS_ALLOCATION` read that way on every alpha. A WARNING that does carry a value is
    the verdict the check will hand down once it resolves, so judge it now."""
    if result == "FAIL":
        return True
    if result != "WARNING" or value is None or limit is None:
        return False
    return value < limit if name.startswith("LOW_") else value > limit


def counts(expr: str) -> tuple[int, int]:
    """Operators and unique data fields, by the Power Pool rules: repeats count, infix counts,
    ts_backfill and the grouping fields are free."""
    if not expr:
        return 0, 0
    body = re.sub(r"\b(ts_backfill|group_backfill)\s*\(", "(", expr)
    ops = len(CALL.findall(body)) + len(INFIX.findall(re.sub(r"\w+\s*=", "", body)))
    fields = {m for m in IDENT.findall(expr)
              if m not in GROUPING and m not in NOT_A_FIELD and not m.isdigit()}
    fields -= set(CALL.findall(expr))
    return ops, len(fields)


def digest(result) -> dict:
    """Flatten one trial.result into stats plus the checks that carry a verdict."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    if not isinstance(result, dict):
        return {}
    stats = result.get("stats") or {}
    checks = {}
    for c in result.get("checks") or []:
        if c.get("name"):
            checks[c["name"]] = (c.get("result"), c.get("value"), c.get("limit"))
    return {"stats": stats, "checks": checks}


def verdicts(stats: dict, checks: dict) -> tuple[str, str]:
    """over_bar against the market's own limits, plus the names of everything failing."""
    bad = sorted(n for n, (r, v, l) in checks.items() if failing(n, r, v, l))

    def lim(name):
        got = checks.get(name)
        return got[2] if got else None

    sh, fit, tvr = stats.get("sharpe"), stats.get("fitness"), stats.get("turnover")
    ls, lf, lt = lim("LOW_SHARPE"), lim("LOW_FITNESS"), lim("HIGH_TURNOVER")
    over = ""
    if None not in (sh, ls) and None not in (fit, lf) and None not in (tvr, lt):
        over = "Y" if (sh >= ls and fit >= lf and tvr <= lt) else ""

    return over, ",".join(bad)


def pp_ok(stats: dict, checks: dict, ops: int, fields: int) -> str:
    if (stats.get("sharpe") or 0) < 1.0 or ops > 8 or fields > 3:
        return ""
    blocking = [n for n, (r, v, l) in checks.items()
                if n not in PP_IGNORES and failing(n, r, v, l)]
    return "" if blocking else "Y"


def load_legacy(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except ValueError:
        return {}


def absorb(src: str, dest: str, known: set[str]) -> int:
    """Keep only the rows this old export is still the only local copy of."""
    rows = json.load(open(src, encoding="utf-8"))
    kept = {}
    for r in rows:
        aid = r.get("alpha_id")
        if not aid or aid in known or r.get("sharpe") is None:
            continue
        ch = r.get("checks") or {}
        kept[aid] = {
            "stats": {k: r.get(k) for k in
                      ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")},
            # The old dumps stored each check as [result, value, limit].
            "checks": {n: (v[0], v[1], v[2]) if isinstance(v, (list, tuple)) and len(v) >= 3
                       else (v if isinstance(v, str) else None, None, None)
                       for n, v in ch.items()},
        }
    json.dump(kept, open(dest, "w", encoding="utf-8"), indent=0)
    return len(kept)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", help="REGION:DELAY, e.g. DEU:0")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--over-bar", action="store_true", help="write only rows clearing their bar")
    ap.add_argument("--absorb-legacy", metavar="PATH",
                    help="distil a pre-lab alphas_*.json into _legacy_metrics.json and exit")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if not os.path.exists(DB):
        sys.exit(f"no database at {DB}")

    con = sqlite3.connect(f"file:{DB.replace(os.sep, '/')}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    metrics: dict[str, dict] = {}
    for r in con.execute("SELECT alpha_id, result FROM trial "
                         "WHERE alpha_id IS NOT NULL AND result IS NOT NULL"):
        d = digest(r["result"])
        if d:
            metrics[r["alpha_id"]] = d

    if args.absorb_legacy:
        n = absorb(args.absorb_legacy, os.path.join(args.out, LEGACY), set(metrics))
        print(f"kept {n} alphas the trial table does not hold -> {args.out}/{LEGACY}")
        return

    legacy = load_legacy(os.path.join(args.out, LEGACY))
    metrics.update({k: v for k, v in legacy.items() if k not in metrics})
    print(f"{len(metrics)} alphas with stored metrics ({len(legacy)} from the legacy export)")

    want = None
    if args.scope:
        reg, _, dly = args.scope.partition(":")
        want = (reg.upper(), int(dly))

    scopes: dict[tuple, list] = defaultdict(list)
    seen: set[str] = set()
    for r in con.execute(
            "SELECT alpha_id, expression, region, delay, universe, payload, status "
            "FROM simulation_record WHERE alpha_id IS NOT NULL ORDER BY id"):
        key = (r["region"], r["delay"])
        if want and key != want:
            continue
        if r["alpha_id"] in seen:
            continue
        seen.add(r["alpha_id"])

        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        settings = (payload or {}).get("settings") or {}

        m = metrics.get(r["alpha_id"], {})
        stats, checks = m.get("stats") or {}, m.get("checks") or {}
        ops, fields = counts(r["expression"])
        over, failed = verdicts(stats, checks)

        scopes[key].append({
            "alpha_id": r["alpha_id"],
            "sharpe": stats.get("sharpe"), "fitness": stats.get("fitness"),
            "turnover": stats.get("turnover"), "returns": stats.get("returns"),
            "drawdown": stats.get("drawdown"), "margin": stats.get("margin"),
            "universe": r["universe"] or settings.get("universe"),
            "neutralization": settings.get("neutralization"),
            "decay": settings.get("decay"), "truncation": settings.get("truncation"),
            "ops": ops, "fields": fields,
            "over_bar": over, "pp_ok": pp_ok(stats, checks, ops, fields),
            "suspect": suspect(stats, r["expression"]),
            "failed": failed, "expression": r["expression"],
        })
    con.close()

    index = []
    for key in sorted(scopes, key=lambda k: -len(scopes[k])):
        rows = sorted(scopes[key], key=lambda x: -(x["sharpe"] or -99))
        if args.over_bar:
            rows = [x for x in rows if x["over_bar"]]
        if not rows:
            continue
        name = f"{key[0]}-d{key[1]}.csv"
        with open(os.path.join(args.out, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        scored = [x for x in rows if x["sharpe"] is not None]
        # An artifact clears the metric checks handsomely, so counting it as a candidate is how
        # a ticker symbol ends up on a shortlist.
        real = [x for x in scored if not x["suspect"]]
        index.append({
            "scope": f"{key[0]} d{key[1]}", "file": name, "n": len(rows), "scored": len(scored),
            "over": sum(1 for x in real if x["over_bar"]),
            "pp": sum(1 for x in real if x["pp_ok"]),
            "sus": sum(1 for x in scored if x["suspect"]),
            "best": max((x["sharpe"] for x in real), default=None),
        })
        print(f"  {name:<16} {len(rows):>6} alphas  {len(scored):>6} scored  "
              f"{index[-1]['over']:>4} over bar  {index[-1]['pp']:>4} PP-eligible  "
              f"{index[-1]['sus']:>4} suspect")

    if want:
        # A scoped run knows about one scope, so writing the index would delete the other 17.
        print(f"\n{sum(r['n'] for r in index)} alphas -> {args.out} (INDEX.md left alone)")
        return

    with open(os.path.join(args.out, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# The vault, per region and delay\n\n"
            "`scope: data` · `status: generated`\n"
            "`tags: vault, alphas, per region, per delay, over bar, power pool eligible`\n"
            f"`updated: {__import__('datetime').date.today()}`\n\n"
            "Regenerate: `backend/.venv/Scripts/python.exe scripts/export_vault.py`\n\n"
            "`over_bar` is measured against **each market's own limits**, read from the check\n"
            "payload — never a hardcoded 2.69. `pp_ok` is sharpe >= 1.0, <= 8 operators,\n"
            "<= 3 data fields and nothing Power-Pool-relevant failing.\n\n"
            "`suspect` flags the artifact signature — a book that never draws down, or big\n"
            "returns on almost no trading. Those are identifiers and metadata, not signals, and\n"
            "the `over bar` / `PP-eligible` counts below are quoted **excluding** them.\n\n"
            "**No column here is a verdict.** Correlation checks are PENDING in every stored\n"
            "payload and `LOW_DURATION` / `LOW_COVERAGE` never appear in one. Resolve a\n"
            "shortlist with `GET /api/alphas/{id}/check`.\n\n"
            "| scope | file | alphas | with metrics | over bar | PP-eligible | suspect | best |\n"
            "|---|---|--:|--:|--:|--:|--:|--:|\n")
        for r in index:
            best = f"{r['best']:.2f}" if r["best"] is not None else "—"
            fh.write(f"| {r['scope']} | [{r['file']}]({r['file']}) | {r['n']} | {r['scored']} "
                     f"| {r['over']} | {r['pp']} | {r['sus']} | {best} |\n")

    print(f"\n{sum(r['n'] for r in index)} alphas across {len(index)} scopes -> {args.out}")


if __name__ == "__main__":
    main()
