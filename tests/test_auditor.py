"""Domain-agnostic auditor core: verifier, consistency, fusion and guardrail."""

from __future__ import annotations

import pytest

from cat_is_judging import (
    PASS_THRESHOLD,
    UNVERIFIED_CEILING,
    Claim,
    FaithfulnessAuditor,
    FaithfulnessViolation,
    Guardrail,
    Report,
    SymbolicVerifier,
    rule,
)
from cat_is_judging.evaluators import ConsistencyScorer, claim_score

# A toy domain: ground truth is a dict of capitals; claims are "capital(country) = city".
TRUTH = {"France": "Paris", "Japan": "Tokyo", "Peru": None}  # None: known country, capital not on record


@rule("capital")
def capital_rule(c: Claim, gt: dict[str, str | None]):
    country = c.subject
    if gt.get(country) is None:
        return "unverifiable", f"no record for {country}"
    ok = gt[country] == c.attributes["city"]
    return ("supported" if ok else "contradicted"), f"record says {gt[country]}"


def cap(country: str, city: str, critical: bool = False, hedged: bool = False) -> Claim:
    return Claim(id=f"{country}", kind="capital", subject=country, attributes={"city": city},
                 text=f"The capital of {country} is {city}", critical=critical, hedged=hedged)


AUDITOR = FaithfulnessAuditor([capital_rule])


# ------------------------------------------------------------------ verifier -----


def test_contradiction_wins_and_unknown_kinds_are_unverifiable() -> None:
    always_yes = rule("capital", name="always_yes")(lambda c, gt: ("supported", "yes"))
    v = SymbolicVerifier([always_yes, capital_rule])
    assert v.verify(cap("France", "Lyon"), TRUTH).outcome == "contradicted"  # one dissent is enough
    assert v.verify(cap("France", "Paris"), TRUTH).outcome == "supported"
    other = Claim(id="x", kind="population", subject="France")
    assert v.verify(other, TRUTH).outcome == "unverifiable"


def test_a_crashing_rule_fails_closed() -> None:
    boom = rule("capital", name="boom")(lambda c, gt: 1 / 0)
    v = SymbolicVerifier([boom]).verify(cap("France", "Paris"), TRUTH)
    assert v.outcome == "unverifiable" and "ZeroDivisionError" in v.evidence


# --------------------------------------------------------- consistency / fusion -----


def test_consistency_is_share_of_samples() -> None:
    s = ConsistencyScorer()
    samples = [Report(id=str(i), claims=(cap("Japan", "Tokyo" if i < 3 else "Kyoto"),)) for i in range(4)]
    assert s.score(cap("Japan", "Tokyo"), samples) == 0.75
    assert s.score(cap("Japan", "Tokyo"), []) is None


def test_symbolic_outranks_consistency() -> None:
    assert claim_score("contradicted", 1.0) == 0.0  # a repeated hallucination is still one
    assert claim_score("supported", 0.0) == 1.0
    assert claim_score("unverifiable", 1.0) == UNVERIFIED_CEILING
    assert claim_score("unverifiable", None) == 0.0


def test_ceiling_tolerates_at_most_ten_percent_unverified() -> None:
    claims = tuple(cap("France", "Paris").model_copy(update={"id": f"f{i}"}) for i in range(9)) + (cap("Peru", "Lima"),)
    r = Report(id="r", claims=claims)
    a = AUDITOR.audit(r, TRUTH, [r] * 3)
    assert a.score == pytest.approx(PASS_THRESHOLD) and a.passed
    worse = Report(id="w", claims=claims[1:] + (cap("Peru", "Lima").model_copy(update={"id": "p2"}),))
    assert not AUDITOR.audit(worse, TRUTH, [worse] * 3).passed


def test_hedging_excuses_missing_evidence_not_contradiction() -> None:
    r = Report(id="r", claims=(cap("France", "Paris"), cap("Peru", "Lima", hedged=True)))
    assert AUDITOR.audit(r, TRUTH).score == 1.0  # the hedged unknown is disclosed, not asserted
    r2 = Report(id="r2", claims=(cap("France", "Paris"), cap("Japan", "Kyoto", hedged=True)))
    assert AUDITOR.audit(r2, TRUTH).score == 0.5


def test_one_false_claim_fails_even_with_a_high_average() -> None:
    claims = tuple(cap("France", "Paris").model_copy(update={"id": f"f{i}"}) for i in range(40)) + (cap("Japan", "Kyoto"),)
    a = AUDITOR.audit(Report(id="r", claims=claims), TRUTH)
    assert a.score > PASS_THRESHOLD and not a.passed and len(a.hallucinations) == 1


# ------------------------------------------------------------------ guardrail -----


GUARD = Guardrail(AUDITOR)


def test_pass_releases_unchanged() -> None:
    r = Report(id="r", claims=(cap("France", "Paris"), cap("Japan", "Tokyo")))
    d = GUARD.review(r, TRUTH)
    assert d.action == "pass" and d.released == r


def test_amend_cuts_contradictions_and_hedges_unknowns() -> None:
    r = Report(id="r", claims=(cap("France", "Paris"), cap("Japan", "Tokyo"), cap("Peru", "Lima"),
                               cap("Japan", "Kyoto").model_copy(update={"id": "bad"})))
    d = GUARD.review(r, TRUTH, [r])
    assert d.action == "amend"
    assert [c.id for c in d.removed] == ["bad"] and [c.id for c in d.hedged] == ["Peru"]
    assert d.released is not None and "[unverified] The capital of Peru is Lima" in d.released.render()
    assert d.amended_audit is not None and d.amended_audit.passed


def test_block_on_critical_contradiction() -> None:
    claims = tuple(cap("France", "Paris").model_copy(update={"id": f"f{i}"}) for i in range(20)) + (cap("Japan", "Kyoto", critical=True),)
    d = GUARD.review(Report(id="r", claims=claims), TRUTH)
    assert d.action == "block" and d.released is None
    with pytest.raises(FaithfulnessViolation) as e:
        GUARD.enforce(Report(id="r", claims=claims), TRUTH)
    assert [c.claim.id for c in e.value.hallucinations] == ["Japan"]


def test_block_when_mostly_unsupported_or_nothing_verified_remains() -> None:
    mostly_wrong = Report(id="r", claims=(cap("France", "Lyon"), cap("Japan", "Kyoto"), cap("Peru", "Lima")))
    assert GUARD.review(mostly_wrong, TRUTH).action == "block"
    only_unknown = Report(id="u", claims=(cap("Peru", "Lima"),))
    d = GUARD.review(only_unknown, TRUTH, [only_unknown])  # score 0.5: not below the block line
    assert d.action == "block" and "nothing verified" in d.reason


def test_intercept_draws_report_then_samples() -> None:
    calls: list[int] = []

    def generate(i: int) -> Report:
        calls.append(i)
        return Report(id=str(i), claims=(cap("France", "Paris"),))

    d = GUARD.intercept(generate, TRUTH, n_samples=4)
    assert calls == [0, 1, 2, 3, 4] and d.audit.n_samples == 4 and d.action == "pass"
