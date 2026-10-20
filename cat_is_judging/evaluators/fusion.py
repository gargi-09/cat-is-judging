"""Neurosymbolic fusion: one support score per claim, one faithfulness score per report.

Symbolic checks outrank consistency. A rule's verdict comes from ground truth, and
consistency only tells you what the generator believes:

* contradicted  -> 0, however consistent: a repeated hallucination is still a hallucination.
* supported     -> 1, however inconsistent: a verified fact is faithful even if the
                   generator was unsure of it.
* unverifiable  -> consistency x UNVERIFIED_CEILING. Consistency is the only evidence left,
                   and it is capped so an unchecked claim can never score like a checked one.
"""

from __future__ import annotations

from cat_is_judging.evaluators.symbolic import Outcome

# The report must reach this to be released unchanged. 0.95 is the faithfulness bar the
# project's CI gate (Phase 4) will enforce.
PASS_THRESHOLD: float = 0.95
# Ceiling for a claim no rule could decide. Chosen with PASS_THRESHOLD: a report scores
# 1 - (1 - ceiling) x (unverified share) at best, so with 0.5 it passes only when at most
# 10% of its claims are unverified, even if every sample repeats them.
UNVERIFIED_CEILING: float = 0.5


def claim_score(outcome: Outcome, consistency: float | None) -> float:
    if outcome == "contradicted":
        return 0.0
    if outcome == "supported":
        return 1.0
    return UNVERIFIED_CEILING * (consistency or 0.0)  # no samples: no evidence at all


def report_score(scores: list[float]) -> float:
    """Mean claim score. A report that asserts nothing is vacuously faithful (1.0)."""
    return sum(scores) / len(scores) if scores else 1.0
