"""Every production-correlation measurement this project has made, keyed by data field.

`PROD_CORRELATION` decides more candidates here than any other check, it is not computed until
you ask for it, and BRAIN rate-limits the asking. It is also — measured 2026-09-20 — a property
of the **field**, not of the dataset, the neutralization or the transform speed
(`kb/method/prod-correlation.md`). So the useful asset is a lookup table: before spending a
correlation request on a candidate, look up what its field scored last time.

Live-check results are **not** written back to `brain_cache` (the cached `alpha:<id>` payload
keeps the pre-check PENDING state), so the measurements cannot be harvested automatically. They
are curated in `MEASURED` below. **Append a row whenever a live check resolves one**, then:

    python scripts/prod_ledger.py            # rewrite .cache/kb/prod-ledger.tsv, print by field
    python scripts/prod_ledger.py --field roe

The expression, region, universe and neutralization are read from the local DB, so a row here is
just an alpha id and the number BRAIN returned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Outside the repo: the packaged app and this checkout share one store.
DATA = os.environ.get("AH_DATA_DIR") or os.path.expanduser("~/.alpha-harness")
DB = os.path.join(DATA, "harness.db")
OUT = os.path.join(ROOT, ".cache", "kb", "prod-ledger.tsv")
LIMIT = 0.70

#: alpha id -> the value BRAIN returned for PROD_CORRELATION on a live check.
MEASURED: dict[str, float] = {
    # 2026-09-20, the model138 field-by-field sweep that established field-level variation
    "Wjbd9ZOO": 0.4194, "0mXdzLxv": 0.4260, "xA3QREgb": 0.6313, "LLN6pW7m": 0.6386,
    "KPN6koJN": 0.6970, "wpZ1Rqr1": 0.7220, "E5pbK3mL": 0.7836, "LLN6kw81": 0.8047,
    "9qj6pleK": 0.7537,   # group_rank, mdl138_pg_3idp
    # 2026-09-20, model138 taken to EUR d1 — the KOR prefix rule does not transfer
    "vRrz2rdr": 0.9352, "ZYblAjzx": 0.7529, "vRrz2kPz": 0.9206,
    # 2026-09-20, ts_target_tvr_hump on the nws29 family
    "vRrzAabz": 0.4947, "mLmzaqR2": 0.5121,
    # 2026-09-20, margin-ranked vault shortlist — 11 of 11 failed
    "A1NbMdwR": 0.7692, "LLN6AngL": 0.8393, "kqVAgJeg": 0.7540, "om6x86Q2": 0.8713,
    "d5ONAm8w": 0.7826, "LL9vQNEM": 0.7682, "d5OMGbYJ": 0.8938, "A10poxvQ": 0.7954,
    "leveYQLA": 0.9559, "9qjZVmRe": 0.8913, "omLvvjbk": 0.4247,
    # 2026-09-20, EUR d1 cross-region siblings
    "GrbVE6O5": 0.6669, "1YXvbvkz": 0.7801,
    # 2026-09-20, the e79jGq36 sub-universe family (CW fails, prod is fine)
    "vRkQrpkQ": 0.4350,
    # submitted, and the shortlists they came from
    "e79YXNPM": 0.5889, "JjNYMlbl": 0.4892, "rK5x0qEJ": 0.4247, "58z9Vroo": 0.6544,
    "78NO9d18": 0.6189,
    "e79jGq36": 0.4934, "O0NkeYAY": 0.3758, "RRbqjX21": 0.4639, "1Yx8rOAR": 0.6138,
    "d5OMJpRx": 0.5706, "QPbeQKQX": 0.6486, "npd98LXE": 0.5989, "O0rL3NRR": 0.6847,
    "VkaV3ARG": 0.4050, "d5OG6M0g": 0.3293, "MPaXo7xn": 0.6932, "WjP57oNP": 0.6500,
    # resolved and dead
    "npdx6kew": 0.7679, "Wjb1YXMk": 0.7848, "A10A29Le": 0.8319, "YP5RGmdA": 0.7034,
    "vRkEkzlr": 0.9372, "O0Ne8aqb": 0.8452, "WjPvbdbZ": 0.9593, "le8ov357": 0.8353,
    "E5p3JmXL": 0.8989, "2rwER0jw": 0.8212,
    # 2026-09-20, the Regular-capable sweep - 12 distinct fields, 12 failures.
    # Every alpha here cleared sharpe, fitness, 2Y, sub-universe, robust and CW first.
    "E5v9pbpG": 0.9551, "O0NQRY5R": 0.9084, "O0NQ8Ybp": 0.8164, "E5pOwgEP": 0.8594,
    "N1axp52o": 0.8233, "rK5ZeaAd": 0.8954, "Vk6A0Q28": 0.8787, "2rO3mVvY": 0.8509,
    "88j5zK2W": 0.9250, "N1ak00GX": 0.8827, "P02Qwl57": 0.9003, "omL8Yg72": 0.8745,
    # 2026-09-20, the strong-2Y prod-clean shortlist - 9 of 9 pass prod, 7 of 9 die on CW
    "akbPR8Wx": 0.3548, "qMxz5Mov": 0.3769, "e7b2aAGp": 0.3943, "A1NaPK8e": 0.4619,
    "1YXmOaYz": 0.3346, "vRrzYvdG": 0.3554, "mLmzbMW5": 0.4604, "akbJO9O6": 0.6705,
    # 2026-09-20, one field, one expression, EUR d1 - universe moves prod by 0.15
    "QPbeaPVK": 0.6875, "xA3mXkjN": 0.8192, "2rwnPdOw": 0.8511,
    # 2026-09-20, total_buy_transaction_count. 9qjAAxJ2 is over 0.70 but BRAIN PASSES it on
    # the sharpe escape clause; 88jKrWJ7 passes outright. JjN5mJNl also CW-fails live.
    "88jKrWJ7": 0.6868, "JjN5mJNl": 0.7880, "qMxmz66E": 0.7511, "9qjAAxJ2": 0.7479,
    # 2026-09-20, one expression, one universe (EUR d1 TOP1200), neutralization the ONLY
    # variable: SLOW 0.5927, SECTOR 0.7479, NONE 0.7533, MARKET 0.7634. Sharpe runs the
    # other way - the 0.59 row is the sharpe-2.88 one.
    "KPNwwJkl": 0.5927, "A1NOOmQg": 0.7533, "akbWWKo1": 0.7634,
}

#: Operator and wrapper tokens, so the first surviving token in an expression is the data field.
#: Prefixes are operator families; the bare words are operators and grouping fields, and they
#: must match exactly — `industry_value_momentum_rank_float` is a data field, not `industry`.
NOT_A_FIELD = re.compile(
    r"^(?:(?:ts_|vec_|group_)\w*"
    r"|(?:rank|zscore|scale|hump|trade_when|divide|subtract|multiply|add|sign|log|power|densify"
    r"|abs|max|min|reverse|winsorize|normalize|quantile|bucket|returns|close|open|high|low|vwap"
    r"|volume|cap|adv20|industry|sector|subindustry|market|country))$")


def field_of(expression: str) -> str:
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", expression or ""):
        if not NOT_A_FIELD.match(token):
            return token
    return "?"


def context(connection: sqlite3.Connection, alpha_id: str) -> dict:
    row = connection.execute(
        "select expression, settings from trial where alpha_id = ? limit 1", (alpha_id,)).fetchone()
    if row:
        settings = json.loads(row["settings"])
        return {"expression": row["expression"], **settings}
    row = connection.execute(
        "select expression, payload from simulation_record where alpha_id = ? "
        "and payload is not null limit 1", (alpha_id,)).fetchone()
    if not row:
        return {}
    settings = (json.loads(row["payload"]).get("settings") or {})
    return {"expression": row["expression"], **settings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--field", help="only rows whose field name contains this")
    args = parser.parse_args()

    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    rows = []
    for alpha_id, prod in MEASURED.items():
        ctx = context(connection, alpha_id)
        if not ctx:
            rows.append((prod, alpha_id, "?", "?", 0, "?", "?", ""))
            continue
        rows.append((prod, alpha_id, field_of(ctx["expression"]), ctx.get("region", "?"),
                     ctx.get("delay", 0), ctx.get("universe", "?"),
                     ctx.get("neutralization", "?"), ctx["expression"]))
    rows.sort()

    with open(OUT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("prod\tverdict\talpha_id\tfield\tregion\tdelay\tuniverse\tneutralization\texpression\n")
        for prod, alpha_id, field, region, delay, universe, neutralization, expression in rows:
            verdict = "PASS" if prod < LIMIT else "FAIL"
            handle.write(f"{prod:.4f}\t{verdict}\t{alpha_id}\t{field}\t{region}\t{delay}\t"
                         f"{universe}\t{neutralization}\t{expression}\n")

    shown = [r for r in rows if not args.field or args.field.lower() in r[2].lower()]
    print(f"{len(rows)} measurements -> {os.path.relpath(OUT, ROOT)}"
          f"   {sum(1 for r in rows if r[0] < LIMIT)} pass, {sum(1 for r in rows if r[0] >= LIMIT)} fail\n")
    print(f"  {'prod':>6}  {'alpha':9} {'scope':8} {'field':38} neutralization")
    for prod, alpha_id, field, region, delay, universe, neutralization, _expression in shown:
        mark = " " if prod < LIMIT else "x"
        print(f"{mark} {prod:6.4f}  {alpha_id:9} {region} d{delay:<5} {field[:38]:38} {neutralization}")


if __name__ == "__main__":
    main()
