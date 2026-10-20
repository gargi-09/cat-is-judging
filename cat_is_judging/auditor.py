"""The faithfulness auditor: symbolic verdict + self-consistency -> one score per report."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cat_is_judging.claims import Claim, Report
from cat_is_judging.evaluators.consistency import Agreement, ConsistencyScorer, same_claim
from cat_is_judging.evaluators.fusion import PASS_THRESHOLD, claim_score, report_score
from cat_is_judging.evaluators.symbolic import Outcome, Rule, RuleResult, SymbolicVerifier


class ClaimAudit(BaseModel):
    claim: Claim
    outcome: Outcome
    rules: list[RuleResult]
    consistency: float | None  # None when no samples were given
    score: float
    # False only for a hedged claim that is merely unverifiable: the report already
    # discloses it as unverified, so it is not held against the score.
    counted: bool

    @property
    def evidence(self) -> str:
        return "; ".join(r.evidence for r in self.rules)


class AuditResult(BaseModel):
    report_id: str
    claims: list[ClaimAudit]
    score: float
    threshold: float
    n_samples: int

    @property
    def hallucinations(self) -> list[ClaimAudit]:
        return [c for c in self.claims if c.outcome == "contradicted"]

    @property
    def critical_failures(self) -> list[ClaimAudit]:
        return [c for c in self.hallucinations if c.claim.critical]

    @property
    def unverified(self) -> list[ClaimAudit]:
        return [c for c in self.claims if c.outcome == "unverifiable"]

    @property
    def supported(self) -> list[ClaimAudit]:
        return [c for c in self.claims if c.outcome == "supported"]

    @property
    def passed(self) -> bool:
        """At or above threshold, and no claim contradicted: a high average can't hide a false fact."""
        return self.score >= self.threshold and not self.hallucinations


class FaithfulnessAuditor:
    def __init__(self, rules: list[Rule], agree: Agreement = same_claim, threshold: float = PASS_THRESHOLD) -> None:
        self.verifier = SymbolicVerifier(rules)
        self.consistency = ConsistencyScorer(agree)
        self.threshold = threshold

    def audit(self, report: Report, ground_truth: Any, samples: list[Report] | tuple[Report, ...] = ()) -> AuditResult:
        """Audit every claim in ``report``; ``samples`` are independent re-generations of it."""
        samples = list(samples)
        audits = []
        for c in report.claims:
            v = self.verifier.verify(c, ground_truth)
            cons = self.consistency.score(c, samples)
            audits.append(ClaimAudit(claim=c, outcome=v.outcome, rules=v.results, consistency=cons,
                                     score=claim_score(v.outcome, cons),
                                     counted=not (c.hedged and v.outcome == "unverifiable")))
        score = report_score([a.score for a in audits if a.counted])
        return AuditResult(report_id=report.id, claims=audits, score=round(score, 4), threshold=self.threshold,
                           n_samples=len(samples))
