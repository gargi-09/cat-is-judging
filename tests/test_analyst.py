"""Phase 3 analyst: tool loop, request shape and the analyst -> auditor -> guardrail path, offline.

A scripted client stands in for the Anthropic API, so these tests cost nothing and never
touch the network; they pin down what the analyst sends and how it handles what comes back.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from applications.maritime_threat_detection import audit as A
from applications.maritime_threat_detection.analyst import (
    FALLBACK_BETA,
    MODEL,
    REPORT_SCHEMA,
    AnalystError,
    ReconAnalyst,
)
from applications.maritime_threat_detection.tools.agent_tools import TOOLS, run_tool
from tests.test_maritime_audit import AIS_B, HULL_A, HULL_B, TILE, gt  # noqa: F401  (gt is a fixture)


def tool_use(id_: str, name: str, **inp: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def text(s: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=s)


def resp(stop: str, *blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=list(blocks), model=MODEL, stop_details=None,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                                                 cache_creation_input_tokens=0))


def report_json(**over: Any) -> str:
    body = {"summary": "s", "vessel_count": 4, "vessels_present": [{"mmsi": 538000001, "lat": HULL_A[0], "lon": HULL_A[1]}],
            "dark_vessel_count": 2, "spoofing_candidates": [{"mmsi": 538000002, "lat": HULL_B[0], "lon": HULL_B[1]}],
            "going_dark_candidates": []}
    return json.dumps(body | over)


class ScriptedClient:
    """``client.beta.messages.create(**kw)`` returns the next scripted response (or calls it with kw)."""

    def __init__(self, script: list[SimpleNamespace | Callable[[dict[str, Any]], SimpleNamespace]]) -> None:
        self.script, self.calls = list(script), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw: Any) -> SimpleNamespace:
        self.calls.append({**kw, "messages": list(kw["messages"])})
        nxt = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return nxt(kw) if callable(nxt) else nxt


# --------------------------------------------------------------------- tools -----


def test_tool_schemas_are_strict_and_complete() -> None:
    for t in TOOLS:
        s = t.schema()
        assert s["strict"] is True and s["input_schema"]["additionalProperties"] is False
        assert set(s["input_schema"]["required"]) == set(s["input_schema"]["properties"])


def test_tools_never_expose_annotator_labels(gt: A.MaritimeGroundTruth) -> None:
    hulls = run_tool(gt, "list_hulls", {"tile_id": TILE})["hulls"]
    assert len(hulls) == 4 and all(set(h) == {"hull", "lat", "lon", "length_m", "axis_bearing_deg"} for h in hulls)


def test_measurement_tools(gt: A.MaritimeGroundTruth) -> None:
    near = run_tool(gt, "compare_hull_to_ais", {"tile_id": TILE, "hull": 0, "mmsi": 538000001})
    assert near["within_tolerance"] and near["distance_m"] < 50
    far = run_tool(gt, "compare_hull_to_ais", {"tile_id": TILE, "hull": 1, "mmsi": 538000002})
    assert not far["within_tolerance"] and far["distance_m"] > 2000
    silent = run_tool(gt, "ais_silence", {"tile_id": TILE, "mmsi": 538000003})
    assert silent["assessment"] == "ais_silent"  # a 3 h gap is insufficient data
    assert run_tool(gt, "check_mmsi", {"mmsi": 970123456})["valid_ship_mmsi"] is False


# ---------------------------------------------------------------------- loop -----


def test_loop_runs_tools_and_returns_an_auditable_report(gt: A.MaritimeGroundTruth) -> None:
    client = ScriptedClient([
        resp("tool_use", text("Looking at the hulls and AIS."), tool_use("t1", "list_hulls", tile_id=TILE),
             tool_use("t2", "ais_near_tile", tile_id=TILE)),
        resp("tool_use", tool_use("t3", "compare_hull_to_ais", tile_id=TILE, hull=0, mmsi=538000001)),
        resp("end_turn", text(report_json())),
    ])
    run = ReconAnalyst(client).investigate(gt, TILE)

    first = client.calls[0]
    assert first["model"] == MODEL and first["fallbacks"] == "default" and first["betas"] == [FALLBACK_BETA]
    assert first["thinking"] == {"type": "adaptive"} and "temperature" not in first and "tool_choice" not in first
    assert first["output_config"]["format"]["schema"] == REPORT_SCHEMA and first["output_config"]["effort"] == "high"
    # Both parallel tool results go back in ONE user message, ids matched, history append-only.
    second = client.calls[1]["messages"]
    assert [r["tool_use_id"] for r in second[-1]["content"]] == ["t1", "t2"]
    assert second[:1] == first["messages"][:1]

    assert [c.name for c in run.trace] == ["list_hulls", "ais_near_tile", "compare_hull_to_ais"] and run.turns == 3
    assert run.usage["cache_read_input_tokens"] == 240
    kinds = [c.kind for c in run.report.claims]
    assert kinds == ["vessel_count", "vessel_present", "dark_vessel_count", "spoofing"]

    d = A.make_guardrail().review(run.report, gt)
    outcome = {c.claim.kind: c.outcome for c in d.audit.claims}
    assert outcome == {"vessel_count": "supported", "vessel_present": "supported",
                       "dark_vessel_count": "unverifiable", "spoofing": "supported"}
    assert d.action == "amend" and [c.kind for c in d.hedged] == ["dark_vessel_count"]


def test_tool_errors_go_back_to_the_model(gt: A.MaritimeGroundTruth) -> None:
    client = ScriptedClient([resp("tool_use", tool_use("t1", "compare_hull_to_ais", tile_id=TILE, hull=99, mmsi=1)),
                             resp("end_turn", text(report_json()))])
    run = ReconAnalyst(client).investigate(gt, TILE)
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "hulls 0..3" in result["content"]
    assert run.trace[0].ok is False


def test_refusal_and_bad_output_raise(gt: A.MaritimeGroundTruth) -> None:
    refused = resp("refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    with pytest.raises(AnalystError, match="declined"):
        ReconAnalyst(ScriptedClient([refused])).investigate(gt, TILE)
    with pytest.raises(AnalystError, match="schema"):
        ReconAnalyst(ScriptedClient([resp("end_turn", text('{"summary": "no counts"}'))])).investigate(gt, TILE)
    with pytest.raises(AnalystError, match="max_tokens"):
        ReconAnalyst(ScriptedClient([resp("max_tokens", text("{"))])).investigate(gt, TILE)


def test_turn_budget_nudges_once_then_gives_up(gt: A.MaritimeGroundTruth) -> None:
    client = ScriptedClient([resp("tool_use", tool_use("t", "get_tile", tile_id=TILE))])  # never stops calling tools
    with pytest.raises(AnalystError, match="no report"):
        ReconAnalyst(client, max_turns=2).investigate(gt, TILE)
    nudge = client.calls[2]["messages"][-1]["content"][-1]
    assert nudge["type"] == "text" and "Submit your report" in nudge["text"] and len(client.calls) == 3


def test_a_hallucinated_ghost_is_cut_by_the_guardrail(gt: A.MaritimeGroundTruth) -> None:
    # The analyst trusts AIS for the spoofer and reports it where its transponder says it is.
    ghost = report_json(vessels_present=[{"mmsi": 538000001, "lat": HULL_A[0], "lon": HULL_A[1]},
                                         {"mmsi": 538000002, "lat": AIS_B[0], "lon": AIS_B[1]}],
                        spoofing_candidates=[])
    analyst = ReconAnalyst(ScriptedClient([resp("end_turn", text(ghost))]))
    runs: list = []
    d = A.make_guardrail().intercept(analyst.generator(gt, TILE, runs), gt, n_samples=2)
    assert len(runs) == 3 and [r.draw for r in runs] == [0, 1, 2]
    removed = [c for c in d.removed if c.kind == "vessel_present"]
    assert d.action == "amend" and [c.attributes["mmsi"] for c in removed] == [538000002]
    # It said the same thing every time: consistency 1.0, still contradicted, still cut.
    ghost_audit = next(c for c in d.audit.claims if c.claim.id == removed[0].id)
    assert ghost_audit.consistency == 1.0 and ghost_audit.score == 0.0
