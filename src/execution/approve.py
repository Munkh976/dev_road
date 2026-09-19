"""
CLI approval. Entry point: `make approve`.

The minimum viable review loop — an HTML report to read plus this to decide.
Upgrade to the Datasette UI (`make serve`) if you notice yourself skipping
weeks, because friction here is what breaks adherence.

Approving records a decision. It does NOT send an order. A separate execution
step reads approved proposals and submits them.

STATUS: stub. Flow is fixed; bodies are next.
"""

from __future__ import annotations

from src.config import load_config


def render_pending(rows) -> str:
    """Format proposals for the terminal, exits first then entries."""
    raise NotImplementedError


def main() -> int:
    cfg = load_config()
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
