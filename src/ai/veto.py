"""
AI veto layer (spec section 7).

Authority: VETO ONLY. This module may not generate signals, select symbols,
size positions, or place orders. Its single job is catching the one failure
mode momentum strategies actually have — loading into a stock that is rising
because of a fixed-price buyout, which then goes nowhere.

Failure behavior is `proceed_and_log`: an outage must not halt trading, and
must not silently approve either. It is recorded as UNAVAILABLE and shows up
in the weekly report.

STATUS: stub. The prompt and schema are fixed; the call is next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from src.config import Config

PROMPT_VERSION = "veto-v1"

# Versioned so the audit trail can attribute a flag to an exact prompt.
VETO_PROMPT = """\
You are reviewing {symbol} for disqualifying corporate events only.

Flag ONLY if the supplied material shows evidence of:
  - a pending acquisition or merger
  - an accounting investigation or restatement
  - a delisting notice
  - a bankruptcy filing or going-concern doubt
  - fraud allegations by a regulator
  - a trading halt

Do NOT flag: earnings misses, analyst downgrades, ordinary bad news,
valuation concerns, or price predictions. Do NOT offer an opinion on whether
the stock will rise or fall. You are not selecting investments.

If the material is insufficient to judge, set flag=false and say so in
reasoning. Absence of evidence is not evidence of a problem.

Material:
{material}

Respond with JSON only:
{{"ticker": "{symbol}", "flag": bool, "severity": "low|medium|high",
  "reasoning": "one or two sentences", "sources": ["url", ...]}}
"""


class ReviewStatus(str, Enum):
    OK = "ok"
    INVALID_SCHEMA = "invalid_schema"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass
class VetoResult:
    symbol: str
    flag: bool | None          # None means the layer could not run
    severity: str | None
    reasoning: str | None
    sources: list[str]
    status: ReviewStatus
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    raw_response: str | None = None

    @property
    def blocks_entry(self) -> bool:
        """An unavailable layer does NOT block. Spec section 7: an outage
        must not halt trading. It is logged and visible instead."""
        return self.flag is True


def parse_response(symbol: str, raw: str) -> VetoResult:
    """Validate the model's JSON against the schema.

    Anything malformed becomes INVALID_SCHEMA with flag=None. The system
    never guesses what the model meant.
    """
    raise NotImplementedError


def review(symbol: str, material: str, cfg: Config) -> VetoResult:
    """Run the veto check for one symbol. Never raises — an exception here
    would halt a pipeline over an advisory layer."""
    raise NotImplementedError


def review_batch(
    symbols: list[str], material: dict[str, str], cfg: Config
) -> dict[str, VetoResult]:
    """Review the proposed holdings. Called once per rebalance, for at most
    `risk.max_positions` symbols."""
    raise NotImplementedError
