"""Self-check: the Settings Sampler's arithmetic.

Run as a script — ``uv run python tests/settings_sampler_check.py``. The figures are
the ones measured from BRAIN for om6RAVLn on 2026-09-16.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alpha_harness.engine.packer import MAX_BATCH, key_of
from alpha_harness.tools.settings_sampler import expand, pairs_for

# The arithmetic this tool exists for, checked against the figures measured from BRAIN
# for om6RAVLn on 2026-09-16.
assert pairs_for(True) == [("OFF", "OFF"), ("OFF", "ON"), ("ON", "OFF")], pairs_for(True)
assert pairs_for(False) == [("OFF", "OFF"), ("ON", "OFF")], pairs_for(False)

measured = {  # region: (markets, neutralizations, position available, alive)
    "USA": (12, 11, True, True),
    "EUR": (10, 12, True, True),
    "ASI": (3, 12, True, True),
    "CHN": (2, 11, False, True),
    "JPN": (2, 11, False, True),
    "IND": (1, 11, False, True),
    "DEU": (2, 11, False, False),
    "GBR": (2, 11, False, False),
}
expected = {"USA": 396, "EUR": 360, "ASI": 108, "CHN": 44, "JPN": 44, "IND": 22}
total = runnable = 0
for region, (markets, neutral, position, alive) in measured.items():
    count = markets * neutral * len(pairs_for(position))
    total += count
    if alive:
        runnable += count
        assert count == expected[region], f"{region}: {count} != {expected[region]}"
assert total == 1062, total
assert runnable == 974, runnable


def check_expand() -> None:
    """The reference Alpha leads a full batch, and the shuffle loses nothing.

    ``expand`` shuffles whole batches so no region waits its turn, and the engine fills from
    the head of the queue. The source Alpha has to go first -- ``seed_trials`` marks trial 0 as
    the reference -- but pulling it out of its own market is what this guards: alone at the
    head it burns a core on a batch of one and strands its nine fellows in a tail at the back.
    """
    universes = ["TOP3000", "TOP1000", "TOPSP500"]
    rows = [
        {
            "region": region,
            "neutralizations": ["INDUSTRY", "SUBINDUSTRY", "MARKET", "SECTOR"],
            "pairs": [{"maxTrade": t, "maxPosition": p} for t, p in pairs_for(region == "USA")],
            "markets": [
                {"region": region, "delay": delay, "universe": universe}
                for delay in (0, 1)
                for universe in universes
            ],
        }
        for region in ("USA", "JPN")
    ]
    source = {
        "expression": "rank(close)",
        "decay": 4,
        "truncation": 0.08,
        "region": "JPN",
        "delay": 1,
        "universe": "TOP1000",
        "neutralization": "SECTOR",
        "maxTrade": "ON",
        "maxPosition": "OFF",
    }
    wanted = sum(
        len(r["markets"]) * len(r["neutralizations"]) * len(r["pairs"])  # type: ignore[arg-type]
        for r in rows
    )

    for _ in range(20):  # the order is random; the head is not
        out = expand(rows, set(), set(), set(), source)
        assert len(out) == wanted, f"{len(out)} requests, expected {wanted}"
        assert len({id(r) for r in out}) == wanted, "a request was duplicated"

        lead = out[0].settings
        assert (lead.region, lead.delay, lead.universe) == ("JPN", 1, "TOP1000"), lead
        assert (lead.neutralization, lead.max_trade, lead.max_position) == ("SECTOR", "ON", "OFF")

        # A batch's children must share the 5-tuple, and the engine packs from the head, so
        # the first ten have to be one legal batch.
        opening = {key_of(r.model_dump(by_alias=True, exclude_none=True)) for r in out[:MAX_BATCH]}
        assert len(opening) == 1, f"the opening batch spans {len(opening)} markets"


check_expand()
print(f"ok  total={total}  runnable={runnable}  expand head packs full")
