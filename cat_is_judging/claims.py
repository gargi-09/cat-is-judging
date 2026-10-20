"""Atomic claims: the unit the auditor judges.

A report is audited claim by claim, never as a whole: one invented fact in an otherwise
correct report must still be found, named and removable. Claims are structured (kind,
subject, attributes) so a rule can check them deterministically; ``text`` is only for
people reading the report.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str  # selects the rules that can check it, e.g. "vessel_present"
    subject: str  # what the claim is about, e.g. "mmsi:538000001"; used to pair claims across samples
    attributes: dict[str, Any] = {}
    text: str = ""
    # A contradicted critical claim blocks the whole report instead of being cut out of it:
    # e.g. an accusation, where a false positive does real harm.
    critical: bool = False
    # The report itself says this is unverified. Hedging excuses missing evidence, never
    # contradicting evidence: a hedged claim that is contradicted still counts against the report.
    hedged: bool = False

    def describe(self) -> str:
        body = self.text or f"{self.kind}({self.subject}, {self.attributes})"
        return f"[unverified] {body}" if self.hedged else body


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    claims: tuple[Claim, ...] = ()
    source: str = ""  # who or what produced it

    def render(self) -> str:
        return "\n".join(f"- {c.describe()}" for c in self.claims)
