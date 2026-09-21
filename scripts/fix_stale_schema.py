"""Drop live columns the models no longer declare, which the app's own migrator will not.

`db/sqlite.migrate` is additive by design: it adds missing tables, columns and indexes and
refuses to remove anything. That is right until a dropped column was NOT NULL with no
default -- then every ORM insert into that table fails the constraint and the route 500s,
which is what `study.sampler_notes` did to task creation.

    python scripts/fix_stale_schema.py            # report only
    python scripts/fix_stale_schema.py --apply    # back up, then drop

Only fatal columns are dropped: absent from the models AND NOT NULL with no default. A
harmless leftover is reported and left alone.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))

from alpha_harness.config import get_settings  # noqa: E402
from alpha_harness.db.models import Base  # noqa: E402

DB = get_settings().sqlite_path


def stale(db: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Live columns the models do not declare, split into fatal and harmless."""
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    fatal: list[tuple[str, str]] = []
    harmless: list[tuple[str, str]] = []
    for name, table in sorted(Base.metadata.tables.items()):
        if name not in tables:
            continue
        declared = set(table.columns.keys())
        for _, column, _, notnull, default, _ in db.execute(f"pragma table_info({name})"):
            if column in declared:
                continue
            (fatal if notnull and default is None else harmless).append((name, column))
    return fatal, harmless


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="drop them; otherwise report only")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}")
        return 1

    db = sqlite3.connect(args.db, timeout=30)
    fatal, harmless = stale(db)
    for table, column in harmless:
        print(f"  leave  {table}.{column}  (nullable, so inserts still work)")
    for table, column in fatal:
        print(f"  DROP   {table}.{column}  (NOT NULL with no default -- every insert fails)")
    if not fatal:
        print("Nothing fatal. The schema matches the models closely enough to write to.")
        return 0
    if not args.apply:
        print("\nReport only. Re-run with --apply to back up and drop.")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup = args.db.with_name(f"{args.db.stem}.{stamp}.backup{args.db.suffix}")
    db.execute("vacuum into ?", (str(backup),))
    print(f"\nBacked up to {backup}")
    for table, column in fatal:
        db.execute(f"alter table {table} drop column {column}")
    db.commit()
    print(f"Dropped {len(fatal)} column{'s' if len(fatal) != 1 else ''}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
