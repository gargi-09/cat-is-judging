"""Self-consistency: does the generator keep making the same claim when re-sampled?

A claim the generator only makes some of the time is weakly held, and in the literature
(e.g. SelfCheckGPT) low sample agreement is a useful hallucination signal. The converse is
not true: a generator can repeat the same wrong claim every time. So consistency is only
ever *evidence of belief*, never of truth, and the fuser treats it that way.
"""

from __future__ import annotations

from typing import Callable

from cat_is_judging.claims import Claim, Report

Agreement = Callable[[Claim, Claim], bool]


def same_claim(a: Claim, b: Claim) -> bool:
    """Default agreement: same kind, subject and attributes. Domains pass a looser test
    (e.g. positions within a tolerance) where exact equality is too strict."""
    return a.kind == b.kind and a.subject == b.subject and a.attributes == b.attributes


class ConsistencyScorer:
    def __init__(self, agree: Agreement = same_claim) -> None:
        self.agree = agree

    def score(self, claim: Claim, samples: list[Report]) -> float | None:
        """Share of independent samples that make an agreeing claim; None without samples."""
        if not samples:
            return None
        hits = sum(1 for s in samples if any(self.agree(claim, c) for c in s.claims))
        return hits / len(samples)
