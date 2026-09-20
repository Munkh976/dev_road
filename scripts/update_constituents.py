#!/usr/bin/env python
"""
Build data/universe/sp500_constituents.csv from a source you review.

Manual, run roughly quarterly. Nothing in the refresh path calls this: IBKR
does not provide index membership, and fetching a list from the web at
runtime would make trading depend on a third-party page (spec section 2.1).

By default this is a DRY RUN. It prints what would change against the current
file; nothing is written until you pass --write. Then read `git diff` and
commit it yourself. The source is your responsibility: check the added and
removed names against something you trust.

    python scripts\\update_constituents.py                        # dry run, default source
    python scripts\\update_constituents.py --write
    python scripts\\update_constituents.py --source my_list.csv --write

The source must be a CSV (URL or local path) with a symbol column (Symbol,
Ticker) and a name column (Security, Name, Company). Symbols are stored the
way index lists write them (BRK.B); the loader converts to IBKR's form
(BRK B) on read.

Default source: the community-maintained datasets/s-and-p-500-companies
repository, which mirrors Wikipedia's constituents table. It carries no
as-of date of its own, so as_of_date is the day you run this.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402

DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)
SYMBOL_ALIASES = ("symbol", "ticker")
NAME_ALIASES = ("security", "name", "company")
# A list far from ~500 names means a bad source or a parse problem.
MIN_EXPECTED, MAX_EXPECTED = 480, 520
OUT_COLUMNS = ["symbol", "name", "as_of_date"]


def normalize_symbol(raw: str) -> str:
    """Canonical index-list form: upper case, share-class separator is a dot."""
    return raw.strip().upper().replace("-", ".")


def parse_source(text: str) -> list[tuple[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    sym_col = next((fields[a] for a in SYMBOL_ALIASES if a in fields), None)
    name_col = next((fields[a] for a in NAME_ALIASES if a in fields), None)
    if sym_col is None or name_col is None:
        raise SystemExit(
            f"source needs a symbol column {SYMBOL_ALIASES} and a name column "
            f"{NAME_ALIASES}; found {list(fields)}"
        )
    rows: dict[str, str] = {}
    for r in reader:
        sym = normalize_symbol(r[sym_col] or "")
        if sym:
            rows[sym] = (r[name_col] or "").strip()
    return sorted(rows.items())


def read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def read_current(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {normalize_symbol(r["symbol"]): r["name"] for r in csv.DictReader(fh)}


def write_csv(path: Path, rows: list[tuple[str, str]], as_of: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(OUT_COLUMNS)
        for sym, name in rows:
            w.writerow([sym, name, as_of.isoformat()])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="CSV URL or local path")
    ap.add_argument("--out", type=Path, help="output path (default: from config.yaml)")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--write", action="store_true", help="write the file (default: dry run)")
    args = ap.parse_args(argv)

    out = args.out or load_config().constituents_path
    rows = parse_source(read_source(args.source))
    if not MIN_EXPECTED <= len(rows) <= MAX_EXPECTED:
        print(f"REFUSING: {len(rows)} symbols from {args.source}; expected "
              f"{MIN_EXPECTED}-{MAX_EXPECTED}. Wrong source, or a parse problem.")
        return 1

    current = read_current(out)
    new = dict(rows)
    added = sorted(set(new) - set(current))
    removed = sorted(set(current) - set(new))
    print(f"source:   {args.source}")
    print(f"symbols:  {len(rows)}   (current file: {len(current)})")
    print(f"added   ({len(added)}):   {', '.join(added) or '-'}")
    print(f"removed ({len(removed)}): {', '.join(removed) or '-'}")

    if not args.write:
        print("\nDry run. Re-run with --write, then review `git diff` before committing.")
        return 0
    write_csv(out, rows, args.as_of)
    print(f"\nWrote {out} (as_of_date {args.as_of}). Review `git diff`, then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
