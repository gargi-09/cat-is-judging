"""The recon analyst: an LLM agent that investigates a satellite tile and writes a report.

This is the thing being audited. It gets the same evidence a human analyst would (the
hulls the satellite saw, AIS tracks, typed measurements) through read-only tools, and
returns a structured report. The report is turned into atomic claims that the Phase 2
auditor checks one by one; nothing here grades its own work.

API choices (Claude Opus 5.5):

* A manual tool loop, so every request and tool call is visible and testable offline with
  a scripted client. The history is append-only (assistant turns are appended unchanged),
  which keeps prompt caching and thinking blocks valid.
* The final report comes from structured output (``output_config.format``), not a forced
  tool call: Opus 5.5 rejects forced ``tool_choice``.
* Thinking is always on and sampling parameters (``temperature``) are rejected, so
  consistency samples are independent re-runs; the model's own run-to-run variation is
  what self-consistency measures.
* Refusals are handled, with server-side fallback (``fallbacks: "default"``) opted in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cat_is_judging import Claim, Report
from applications.maritime_threat_detection import audit as A
from applications.maritime_threat_detection.tools.agent_tools import TOOLS, ToolError, run_tool

MODEL = "claude-opus-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT: Effort = "high"  # Opus 5.5 defaults to medium; this is analysis, not chat
MAX_TURNS = 16  # tool-use round trips before the analyst is asked to submit what it has
MAX_TOKENS = 16_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are a maritime intelligence analyst. You investigate one Sentinel-2 satellite tile at a \
time and report what is in it, using the read-only tools provided.

The satellite image is the authority on where hulls physically are. AIS transponder reports are claims by the \
ships themselves: they can be stale, missing, or false. Identity comes only from AIS, so a hull with no matching \
AIS track has no known identity.

Your report is checked claim by claim against the imagery and the AIS record, so report what the evidence \
supports. When the evidence is insufficient (an AIS gap, a hull no track explains), leave the claim out and say \
so in the summary instead of guessing. Accusations (spoofing, going dark) are serious: make one only when the \
tools show it, and treat each as a candidate for human review, not a conclusion.

Report fields:
- vessel_count: hulls the satellite detected in the tile.
- vessels_present: ships you identified, each with the MMSI and the position of the hull you matched it to.
- dark_vessel_count: hulls of 30 m or more that no AIS track can explain.
- spoofing_candidates: ships whose AIS position disagrees with a hull you are confident is them, with that hull's position.
- going_dark_candidates: ships the ais_silence tool assesses as potential going dark.
- summary: two or three sentences, including anything you could not resolve."""

_POSITION = {"type": "object", "additionalProperties": False, "required": ["mmsi", "lat", "lon"],
             "properties": {"mmsi": {"type": "integer"}, "lat": {"type": "number"}, "lon": {"type": "number"}}}
REPORT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "vessel_count", "vessels_present", "dark_vessel_count", "spoofing_candidates",
                 "going_dark_candidates"],
    "properties": {
        "summary": {"type": "string"},
        "vessel_count": {"type": "integer"},
        "vessels_present": {"type": "array", "items": _POSITION},
        "dark_vessel_count": {"type": "integer"},
        "spoofing_candidates": {"type": "array", "items": _POSITION},
        "going_dark_candidates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["mmsi"],
            "properties": {"mmsi": {"type": "integer"}}}},
    },
}


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mmsi: int
    lat: float
    lon: float


class Mmsi(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mmsi: int


class AnalystOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    vessel_count: int = Field(ge=0)
    vessels_present: list[Position]
    dark_vessel_count: int = Field(ge=0)
    spoofing_candidates: list[Position]
    going_dark_candidates: list[Mmsi]

    def to_report(self, tile_id: str, report_id: str) -> Report:
        """Atomic claims for the auditor. Duplicates (same claim twice) are kept once."""
        claims: list[Claim] = [A.vessel_count(tile_id, self.vessel_count)]
        claims += [A.vessel_present(tile_id, v.mmsi, v.lat, v.lon) for v in self.vessels_present]
        claims.append(A.dark_vessel_count(tile_id, self.dark_vessel_count))
        claims += [A.spoofing(tile_id, v.mmsi, v.lat, v.lon) for v in self.spoofing_candidates]
        claims += [A.going_dark(tile_id, v.mmsi) for v in self.going_dark_candidates]
        unique = list({c.id: c for c in claims}.values())
        return Report(id=report_id, claims=tuple(unique), source=f"analyst:{MODEL}")


class AnalystError(RuntimeError):
    """The run produced no usable report (refusal, truncation, invalid output, turn budget)."""


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    ok: bool
    result_preview: str


@dataclass
class AnalystRun:
    tile_id: str
    draw: int
    output: AnalystOutput
    report: Report
    trace: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    served_by: str = MODEL  # differs when a refusal fallback answered


def default_client() -> Any:
    """Anthropic client from the environment (ANTHROPIC_API_KEY or an `ant auth login` profile)."""
    import anthropic  # optional dependency: only needed for live runs

    return anthropic.Anthropic()


class ReconAnalyst:
    def __init__(self, client: Any | None = None, model: str = MODEL, effort: Effort = DEFAULT_EFFORT,
                 max_turns: int = MAX_TURNS) -> None:
        self._client = client
        self.model, self.effort, self.max_turns = model, effort, max_turns

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = default_client()
        return self._client

    def _request(self, messages: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[t.schema() for t in TOOLS],
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
            cache_control={"type": "ephemeral"},  # system + tools + growing history are a stable prefix
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def investigate(self, gt: A.MaritimeGroundTruth, tile_id: str, draw: int = 0) -> AnalystRun:
        if gt.tile(tile_id) is None:
            raise ValueError(f"unknown tile {tile_id!r}")
        messages: list[dict[str, Any]] = [{"role": "user", "content": (
            f"Investigate tile {tile_id} and write your report.")}]
        trace: list[ToolCall] = []
        usage: dict[str, int] = {}
        nudged = False
        for turn in range(1, self.max_turns + 2):
            resp = self._request(messages)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[k] = usage.get(k, 0) + (getattr(resp.usage, k, 0) or 0)
            if resp.stop_reason == "refusal":
                details = getattr(resp, "stop_details", None)
                raise AnalystError(f"model declined (category {getattr(details, 'category', None)})")
            if resp.stop_reason == "max_tokens":
                raise AnalystError("response hit max_tokens before the report was complete")
            messages.append({"role": "assistant", "content": resp.content})  # append-only, unchanged

            uses = [b for b in resp.content if b.type == "tool_use"]
            if uses:
                results = []
                for b in uses:
                    try:
                        out, ok = run_tool(gt, b.name, dict(b.input)), True
                    except ToolError as e:
                        out, ok = {"error": str(e)}, False
                    text = json.dumps(out, default=str)
                    trace.append(ToolCall(b.name, dict(b.input), ok, text[:300]))
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": text, "is_error": not ok})
                if turn >= self.max_turns and not nudged:
                    results.append({"type": "text", "text": "You have used your tool budget. Submit your report now "
                                                            "with what you have verified."})
                    nudged = True
                elif turn > self.max_turns:
                    raise AnalystError(f"no report after {self.max_turns} tool rounds")
                messages.append({"role": "user", "content": results})  # all results in one message
                continue

            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                output = AnalystOutput.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as e:
                raise AnalystError(f"report did not match the schema: {e}") from None
            report = output.to_report(tile_id, f"{tile_id}/analyst/draw{draw}")
            return AnalystRun(tile_id, draw, output, report, trace, turn, usage, getattr(resp, "model", self.model))
        raise AnalystError(f"no report after {self.max_turns} tool rounds")

    def generator(self, gt: A.MaritimeGroundTruth, tile_id: str,
                  runs: list[AnalystRun] | None = None) -> Callable[[int], Report]:
        """For ``Guardrail.intercept``: draw 0 is the report, 1..n are independent re-runs."""

        def generate(draw: int) -> Report:
            run = self.investigate(gt, tile_id, draw)
            if runs is not None:
                runs.append(run)
            return run.report

        return generate
