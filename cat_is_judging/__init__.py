"""Cat-is-Judging: a domain-agnostic, neurosymbolic faithfulness auditor.

A report is split into atomic claims. Each claim is checked by deterministic rules against
ground truth (supported / contradicted / unverifiable) and by agreement across independent
re-generations; the two are fused into one score, and a guardrail passes, amends or blocks
the report. Domains plug in by supplying rules and a ground-truth object.
"""

from cat_is_judging.auditor import AuditResult, ClaimAudit, FaithfulnessAuditor
from cat_is_judging.claims import Claim, Report
from cat_is_judging.evaluators import (
    PASS_THRESHOLD,
    UNVERIFIED_CEILING,
    ConsistencyScorer,
    Rule,
    RuleResult,
    SymbolicVerifier,
    rule,
)
from cat_is_judging.guardrails import BLOCK_BELOW, FaithfulnessViolation, GuardDecision, Guardrail

__all__ = [
    "AuditResult", "BLOCK_BELOW", "Claim", "ClaimAudit", "ConsistencyScorer", "FaithfulnessAuditor",
    "FaithfulnessViolation", "GuardDecision", "Guardrail", "PASS_THRESHOLD", "Report", "Rule", "RuleResult",
    "SymbolicVerifier", "UNVERIFIED_CEILING", "rule",
]
