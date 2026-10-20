"""Guardrail: decide what happens to a report after it is audited — pass, amend or block.

* **pass**  — at or above threshold and nothing contradicted: released unchanged.
* **amend** — contradicted claims are cut and unverifiable ones are marked "[unverified]",
  so the released report asserts only what ground truth supports and discloses the rest.
* **block** — a critical claim (e.g. an accusation) is contradicted, the report is mostly
  unsupported, or nothing verified would survive amendment. Nothing is released.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel

from cat_is_judging.auditor import AuditResult, FaithfulnessAuditor
from cat_is_judging.claims import Claim, Report

Action = Literal["pass", "amend", "block"]

# Below this the average claim is more likely unsupported than supported; cutting and
# hedging would hand the reader a fragment that no longer says what the report meant.
BLOCK_BELOW: float = 0.5


class GuardDecision(BaseModel):
    action: Action
    reason: str
    audit: AuditResult
    released: Report | None = None  # what the reader gets; None when blocked
    removed: list[Claim] = []
    hedged: list[Claim] = []
    amended_audit: AuditResult | None = None


class FaithfulnessViolation(Exception):
    def __init__(self, decision: GuardDecision) -> None:
        self.decision = decision
        self.hallucinations = decision.audit.hallucinations
        super().__init__(f"report {decision.audit.report_id} blocked: {decision.reason}")


class Guardrail:
    def __init__(self, auditor: FaithfulnessAuditor, block_below: float = BLOCK_BELOW) -> None:
        self.auditor = auditor
        self.block_below = block_below

    def review(self, report: Report, ground_truth: Any, samples: list[Report] | tuple[Report, ...] = ()) -> GuardDecision:
        a = self.auditor.audit(report, ground_truth, samples)
        if a.critical_failures:
            names = ", ".join(c.claim.id for c in a.critical_failures)
            return GuardDecision(action="block", audit=a,
                                 reason=f"{len(a.critical_failures)} critical claim(s) contradicted by ground truth ({names})")
        if a.score < self.block_below:
            return GuardDecision(action="block", audit=a,
                                 reason=f"score {a.score:.3f} < {self.block_below}: most of the report is unsupported")
        if a.passed:
            return GuardDecision(action="pass", audit=a, released=report,
                                 reason=f"score {a.score:.3f} >= {a.threshold} with no contradicted claims")

        removed = [c.claim for c in a.hallucinations]
        to_hedge = [c.claim for c in a.unverified if not c.claim.hedged]
        hedge_ids = {c.id for c in to_hedge}
        drop_ids = {c.id for c in removed}
        kept = tuple(c.model_copy(update={"hedged": True}) if c.id in hedge_ids else c
                     for c in report.claims if c.id not in drop_ids)
        amended = report.model_copy(update={"id": f"{report.id}+amended", "claims": kept})
        aa = self.auditor.audit(amended, ground_truth, samples)
        if not aa.supported:
            return GuardDecision(action="block", audit=a, removed=removed, hedged=to_hedge, amended_audit=aa,
                                 reason="nothing verified would remain after amendment")
        return GuardDecision(action="amend", audit=a, released=amended, removed=removed, hedged=to_hedge,
                             amended_audit=aa,
                             reason=f"score {a.score:.3f} < {a.threshold}: removed {len(removed)} contradicted, "
                                    f"marked {len(to_hedge)} unverified")

    def enforce(self, report: Report, ground_truth: Any, samples: list[Report] | tuple[Report, ...] = ()) -> Report:
        """Return the releasable report (original or amended), or raise FaithfulnessViolation."""
        d = self.review(report, ground_truth, samples)
        if d.released is None:
            raise FaithfulnessViolation(d)
        return d.released

    def intercept(self, generate: Callable[[int], Report], ground_truth: Any, n_samples: int = 5) -> GuardDecision:
        """Call ``generate(0)`` for the report and ``generate(1..n)`` for consistency samples."""
        report = generate(0)
        return self.review(report, ground_truth, [generate(i) for i in range(1, n_samples + 1)])
