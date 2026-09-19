"""
Weekly HTML report. Entry point: `make report`.

Written to reports/YYYY-MM-DD.html and readable on a phone. What it must
contain, in this order:

  1. Regime      risk-on or risk-off, and for how long
  2. Exits       any position tripping X1-X4, with the rule that fired
  3. Entries     candidates with rank, score, proposed size
  4. Risk        checks run, anything rejected and why
  5. AI veto     flags, or UNAVAILABLE if the layer failed
  6. Portfolio   live from IBKR: positions, cash, drawdown vs peak
  7. Adherence   overrides in the last 4 weeks (kill criterion at 3 in a row)

STATUS: stub. Contract is fixed; the template is next.
"""

from __future__ import annotations

from pathlib import Path

from src.config import Config, load_config


def build(cfg: Config, run_id: str) -> Path:
    raise NotImplementedError


def main() -> int:
    cfg = load_config()
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
