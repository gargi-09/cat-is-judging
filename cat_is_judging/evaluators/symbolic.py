"""Symbolic verification: deterministic rules checked against ground truth.

Every rule answers one of three things about a claim: the ground truth *supports* it,
*contradicts* it, or cannot decide (*unverifiable*). The third answer is first-class, the
same principle as the track layer returning "can't place it": a rule that guesses when the
data is silent would be inventing exactly the kind of fact the auditor exists to catch.

The core knows nothing about any domain. A domain (e.g. maritime) registers rules for the
claim kinds it understands and passes its own ground-truth object through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel

from cat_is_judging.claims import Claim

Outcome = Literal["supported", "contradicted", "unverifiable"]


class RuleResult(BaseModel):
    rule: str
    outcome: Outcome
    evidence: str


@dataclass(frozen=True)
class Rule:
    name: str
    kinds: frozenset[str]
    check: Callable[[Claim, Any], tuple[Outcome, str]]


def rule(*kinds: str, name: str | None = None) -> Callable[[Callable[[Claim, Any], tuple[Outcome, str]]], Rule]:
    """Decorator: turn ``fn(claim, ground_truth) -> (outcome, evidence)`` into a Rule."""

    def wrap(fn: Callable[[Claim, Any], tuple[Outcome, str]]) -> Rule:
        return Rule(name or fn.__name__, frozenset(kinds), fn)

    return wrap


class SymbolicVerdict(BaseModel):
    outcome: Outcome
    results: list[RuleResult]

    @property
    def evidence(self) -> str:
        return "; ".join(r.evidence for r in self.results)


class SymbolicVerifier:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = list(rules)

    def verify(self, claim: Claim, ground_truth: Any) -> SymbolicVerdict:
        """Run every rule that covers ``claim.kind`` and combine their outcomes.

        Any contradiction wins (one failed check is enough to call a claim false, and two
        rules disagreeing is itself a reason not to trust it); otherwise any support wins;
        otherwise the claim is unverifiable. No applicable rule also means unverifiable, and
        so does a rule that raises: the verifier fails closed, never open.
        """
        results: list[RuleResult] = []
        for r in self.rules:
            if claim.kind not in r.kinds:
                continue
            try:
                outcome, evidence = r.check(claim, ground_truth)
            except Exception as e:  # a broken rule must not let a claim through
                outcome, evidence = "unverifiable", f"rule error ({type(e).__name__}: {e})"
            results.append(RuleResult(rule=r.name, outcome=outcome, evidence=evidence))
        if not results:
            return SymbolicVerdict(outcome="unverifiable", results=[RuleResult(
                rule="<none>", outcome="unverifiable", evidence=f"no rule checks claims of kind {claim.kind!r}")])
        outcomes = {r.outcome for r in results}
        final: Outcome = ("contradicted" if "contradicted" in outcomes
                          else "supported" if "supported" in outcomes else "unverifiable")
        return SymbolicVerdict(outcome=final, results=results)
