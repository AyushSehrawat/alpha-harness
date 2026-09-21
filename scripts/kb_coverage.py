"""Track which BRAIN doc pages have been mined into the knowledge base.

Claiming "I read all the docs" turned out to be wrong twice. Citation is the only
mechanical evidence a page was used, so this regenerates a ledger on disk from the
citations actually present in .cache/kb/ and refuses to let a new page go unnoticed.

    python scripts/kb_coverage.py            # regenerate the ledger, print a summary
    python scripts/kb_coverage.py --check    # exit 1 if anything is UNREAD (CI-ish)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / ".cache" / "docs"
KB = ROOT / ".cache" / "kb"
LEDGER = KB / "docs-coverage.tsv"

# docs-map.md names every page by construction, so counting it would make the audit vacuous.
EXCLUDE_FROM_CITATIONS = {"docs-map.md"}

HEADER = ["page", "category", "figures", "status", "cited_by"]

MINED = "mined"
READ_EMPTY = "read-empty"
UNREAD = "UNREAD"

IMG_RE = re.compile(r"!\[[^\]]*\]\(\.\./_img/[^)]+\)")


def doc_pages() -> list[tuple[str, str, int]]:
    """Every cached doc page as (page_id, category, figure_count)."""
    out = []
    for path in sorted(DOCS.glob("*/*.md")):
        if path.name == "INDEX.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        figures = len(IMG_RE.findall(text)) + text.count("_[IMAGE]_")
        out.append((path.stem, path.parent.name, figures))
    return out


def kb_texts() -> dict[str, str]:
    return {
        str(p.relative_to(KB)).replace("\\", "/"): p.read_text(encoding="utf-8", errors="replace")
        for p in KB.rglob("*.md")
        if p.name not in EXCLUDE_FROM_CITATIONS
    }


def load_ledger() -> dict[str, dict[str, str]]:
    if not LEDGER.exists():
        return {}
    rows = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines()[1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        cells += [""] * (len(HEADER) - len(cells))
        rows[cells[0]] = dict(zip(HEADER, cells))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if any page is UNREAD")
    args = ap.parse_args()

    if not DOCS.is_dir():
        print(f"no docs cache at {DOCS}", file=sys.stderr)
        return 2

    previous = load_ledger()
    texts = kb_texts()
    rows = []

    for page, category, figures in doc_pages():
        tail = page.split("__")[-1]
        cited = sorted(name for name, body in texts.items() if tail in body)
        prior = previous.get(page, {})
        # A hand-set status survives regeneration; a citation always wins over it.
        if cited:
            status = MINED
        elif prior.get("status") in (READ_EMPTY, MINED):
            status = READ_EMPTY
        else:
            status = UNREAD
        rows.append([page, category, str(figures), status, ",".join(cited)])

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(
        "\t".join(HEADER) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    counts = {MINED: 0, READ_EMPTY: 0, UNREAD: 0}
    for r in rows:
        counts[r[3]] = counts.get(r[3], 0) + 1
    total_figs = sum(int(r[2]) for r in rows)

    print(f"{len(rows)} pages, {total_figs} figure references -> {LEDGER.relative_to(ROOT)}")
    print(f"  mined      {counts[MINED]:3d}   cited by at least one kb file")
    print(f"  read-empty {counts[READ_EMPTY]:3d}   read, nothing this project can act on")
    print(f"  UNREAD     {counts[UNREAD]:3d}")
    for r in rows:
        if r[3] == UNREAD:
            print(f"      {r[0]}  ({r[2]} fig)")

    if args.check and counts[UNREAD]:
        print("\nUNREAD pages present - read them and cite them, or mark read-empty.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
