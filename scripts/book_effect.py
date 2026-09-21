"""What a day's shortlist does to the submitted book, after cost.

`GET /api/alphas/{id}/performance` answers for ONE Alpha against ONE partition, so a batch of
four is never the sum of its rows. `POST /api/portfolio/compute` combines any set of Alphas --
submitted or not, as long as the vault holds their daily PnL -- at equal weight, which is how
BRAIN combines its own pool. This drives that endpoint: baseline, one-at-a-time marginals, and
a forward search for the best batch of N.

    python scripts/book_effect.py --candidates a.txt --pick 4
    python scripts/book_effect.py LLN6pW7m e79jGq36 1Yx8rOAR --pick 2 --objective test_sharpe
    python scripts/book_effect.py --batch LLN6pW7m,e79jGq36 --batch O0NkeYAY,RRbqjX21

The default objective is the Test-Period after-cost Sharpe, because that is the block that goes
negative first and the one in-sample margin misranks (`kb/method/turnover-and-fitness.md`).

**The search is correlation-guarded.** A batch that maximises the book's numbers will happily
pick two siblings of one sweep, and `SELF_CORRELATION` is measured against everything already
submitted -- so yesterday's alternates die the moment their sibling ships. Every candidate is
scored against the whole submitted pool and against the rest of the batch before it is allowed
in; `--limit 1` turns the guard off.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
COST_BPS = 5.0
#: BRAIN's SELF_CORRELATION cutoff, measured against every Alpha already submitted.
LIMIT = 0.70
#: Below this many shared trading days a correlation is not worth believing.
MIN_DAYS = 60
#: Every objective is read off `afterCost`; a gross number is not what the pool is paid on.
OBJECTIVES = {
    "test_sharpe": ("test", "sharpe"),
    "test_margin": ("test", "margin"),
    "test_pnl": ("test", "pnl"),
    "is_sharpe": ("inSample", "sharpe"),
    "is_margin": ("inSample", "margin"),
}


def call(path: str, body: dict | None = None, timeout: int = 600) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if body is not None else "GET",
        headers={"content-type": "application/json", "x-harness-client": "1"},
    )
    return json.load(urllib.request.urlopen(request, timeout=timeout))


def compute(ids: list[str], cost: float) -> dict | None:
    try:
        return call("/api/portfolio/compute", {"alpha_ids": ids, "cost_bps": cost})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  compute failed: {type(exc).__name__} {exc}", file=sys.stderr)
        return None


def score(result: dict, objective: str) -> float:
    block, field = OBJECTIVES[objective]
    return result["afterCost"][block][field]


def line(tag: str, result: dict, base: dict | None = None, block: str = "afterCost") -> str:
    """One row in the shape of the Portfolio page's two panels: `stats` and `afterCost`."""
    inside, test = result[block]["inSample"], result[block]["test"]
    out = (f"{tag:26} IS {inside['sharpe']:6.2f}  TEST {test['sharpe']:6.2f}  "
           f"IS mgn {inside['margin'] * 10_000:6.2f}b  TEST mgn {test['margin'] * 10_000:6.2f}b  "
           f"dd {inside['drawdown']:.4f}  TEST PnL {test['pnl']:>12,.0f}")
    if base:
        out += (f"   d TEST {test['sharpe'] - base[block]['test']['sharpe']:+.2f}"
                f" / {(test['margin'] - base[block]['test']['margin']) * 10_000:+.2f}b")
    return out


def members(include_usa: bool) -> list[str]:
    body = json.load(urllib.request.urlopen(f"{BASE}/api/portfolio/members", timeout=180))
    return [m["alphaId"] for m in body["members"]
            if (include_usa or m["region"] != "USA") and m.get("hasSeries", True)]


def submitted() -> list[str]:
    body = json.load(urllib.request.urlopen(f"{BASE}/api/portfolio/members", timeout=180))
    return [m["alphaId"] for m in body["members"] if m.get("hasSeries", True)]


def daily(alpha_id: str) -> dict[str, float] | None:
    """Daily PnL by date, differenced back from the stored cumulative series."""
    try:
        body = json.load(urllib.request.urlopen(
            f"{BASE}/api/vault/alphas/{alpha_id}/detail", timeout=300))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    pnl, dates = body.get("pnl") or [], body.get("dates") or []
    if len(pnl) < 2:
        return None
    return {dates[i]: pnl[i] - pnl[i - 1] for i in range(1, len(pnl))}


def correlation(x: dict[str, float], y: dict[str, float]) -> float | None:
    shared = sorted(set(x) & set(y))
    if len(shared) < MIN_DAYS:
        return None
    a, b = [x[d] for d in shared], [y[d] for d in shared]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    dev_a = sum((v - mean_a) ** 2 for v in a) ** 0.5
    dev_b = sum((v - mean_b) ** 2 for v in b) ** 0.5
    if not dev_a or not dev_b:
        return None
    return sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(len(a))) / (dev_a * dev_b)


class Guard:
    """Keeps a batch inside SELF_CORRELATION, against the submitted pool and against itself."""

    def __init__(self, candidates: list[str], limit: float):
        self.limit = limit
        self.series = {a: s for a in candidates if (s := daily(a)) is not None}
        self.worst: dict[str, tuple[float, str]] = {}
        if limit >= 1:
            return
        for alpha_id in submitted():
            live = daily(alpha_id)
            if not live:
                continue
            for candidate, series in self.series.items():
                value = correlation(series, live)
                if value is not None and abs(value) > abs(self.worst.get(candidate, (0, ""))[0]):
                    self.worst[candidate] = (value, alpha_id)

    def blocked(self, candidate: str) -> str | None:
        value, against = self.worst.get(candidate, (0.0, ""))
        return f"{value:+.3f} vs submitted {against}" if abs(value) > self.limit else None

    def collides(self, candidate: str, chosen: list[str]) -> str | None:
        for other in chosen:
            if candidate in self.series and other in self.series:
                value = correlation(self.series[candidate], self.series[other])
                if value is not None and abs(value) > self.limit:
                    return f"{value:+.3f} vs {other}"
        return None


def read_candidates(args: argparse.Namespace) -> list[str]:
    ids = list(args.alpha_ids)
    if args.candidates and os.path.exists(args.candidates):
        for raw in open(args.candidates, encoding="utf-8"):
            token = raw.split("#")[0].strip()
            if token:
                ids.append(token)
    return list(dict.fromkeys(ids))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("alpha_ids", nargs="*")
    parser.add_argument("--candidates", help="file of candidate ids, one per line")
    parser.add_argument("--batch", action="append", default=[],
                        help="comma-separated batch to price as a unit, repeatable")
    parser.add_argument("--pick", type=int, default=0, help="forward-search a batch of this size")
    parser.add_argument("--exhaustive", action="store_true",
                        help="score every combination of --pick instead of a forward search")
    parser.add_argument("--objective", choices=sorted(OBJECTIVES), default="test_sharpe")
    parser.add_argument("--cost", type=float, default=COST_BPS)
    parser.add_argument("--include-usa", action="store_true")
    parser.add_argument("--limit", type=float, default=LIMIT,
                        help="SELF_CORRELATION cutoff; 1 turns the guard off")
    args = parser.parse_args()

    pool = members(args.include_usa)
    base = compute(pool, args.cost)
    if not base:
        raise SystemExit("the book itself would not compute - is the harness running?")
    scope = "whole book" if args.include_usa else "non-USA book"
    print(f"\n{scope}: {len(pool)} Alphas, cost {args.cost:g} bps, objective {args.objective}\n")
    print(line("baseline", base))

    candidates = read_candidates(args)
    guard = Guard(candidates, args.limit)
    if candidates:
        print("\none at a time:")
        marginal = []
        for alpha_id in candidates:
            if (why := guard.blocked(alpha_id)):
                print(f"{alpha_id:26} BLOCKED  self-correlation {why}")
                continue
            result = compute(pool + [alpha_id], args.cost)
            if result and result["missing"]:
                print(f"{alpha_id:26} no stored series")
                continue
            if result:
                marginal.append((score(result, args.objective), alpha_id, result))
        for _value, alpha_id, result in sorted(marginal, reverse=True):
            print(line(f"+ {alpha_id}", result, base))
        candidates = [a for _v, a, _r in marginal]

    for raw in args.batch:
        batch = [b.strip() for b in raw.split(",") if b.strip()]
        result = compute(pool + batch, args.cost)
        if result:
            print(f"\n{'':26} -- Estimated After-Cost Performance --")
            print(line("baseline", base))
            print(line("+ " + ",".join(batch), result, base))
            print(f"{'':26} -- Combined Alpha Performance (gross) --")
            print(line("baseline", base, block="stats"))
            print(line("+ " + ",".join(batch), result, base, block="stats"))

    if args.pick and candidates:
        print(f"\nbest batch of {args.pick} by {args.objective}:")
        if args.exhaustive:
            best = max(
                ((score(r, args.objective), combo, r) for combo in
                 itertools.combinations(candidates, args.pick)
                 if not any(guard.collides(c, [o for o in combo if o != c]) for c in combo)
                 if (r := compute(pool + list(combo), args.cost))),
                default=None)
            chosen = list(best[1]) if best else []
            result = best[2] if best else None
        else:
            chosen, result = [], None
            while len(chosen) < args.pick:
                step = [(score(r, args.objective), a, r) for a in candidates if a not in chosen
                        if not guard.collides(a, chosen)
                        if (r := compute(pool + chosen + [a], args.cost))]
                if not step:
                    break
                value, alpha_id, result = max(step)
                chosen.append(alpha_id)
                print(line(f"  {len(chosen)}. + {alpha_id}", result, base))
        if result:
            print()
            print(line("chosen " + ",".join(chosen), result, base))


if __name__ == "__main__":
    main()
