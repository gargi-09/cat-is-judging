"""Maritime plug-in for the faithfulness auditor: claim kinds, rules and ground truth.

The authority split follows the project's premise. A ship can falsify its transponder, not
what the satellite sees, so:

* **presence** ("a ship is at P") is decided by the imagery: a hull is there or it isn't;
* **identity** ("that ship is MMSI X") is decided by the identity-verification rules in
  ``reconciliation.py``, and is often only *unverifiable*: an AIS gap, or an MMSI that is
  simply not in this feed ("not in my feed" is not "unregistered").

Every rule reuses the Phase 1.5 thresholds; none is introduced here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cat_is_judging import Claim, FaithfulnessAuditor, Guardrail, rule
from cat_is_judging.evaluators.symbolic import Outcome
from applications.maritime_threat_detection.geo import haversine_m
from applications.maritime_threat_detection.reconciliation import (
    CORROBORATION_BASE_M,
    AISLookup,
    BlindResult,
    TrackPosition,
    Verification,
    assess_silence,
    associate_tile,
    classify_detection,
    inside_chip,
    validate_mmsi,
)

# A position in a report carries no uncertainty of its own, so "a hull is there" uses the
# same 500 m base radius that corroboration uses (~2x the p90 AIS-vs-hull residual).
PRESENCE_RADIUS_M: float = CORROBORATION_BASE_M
# Two samples place the same ship "in the same spot" within that same radius.
SAMPLE_POSITION_AGREEMENT_M: float = CORROBORATION_BASE_M

# Accusations and security conclusions: a false one does real harm, so a contradicted one
# blocks the whole report rather than being quietly cut from it.
CRITICAL_KINDS: frozenset[str] = frozenset({"spoofing", "going_dark", "dark_vessel_count"})
POSITIONAL_KINDS: frozenset[str] = frozenset({"vessel_present", "spoofing"})


class MaritimeGroundTruth:
    """Read-only view of the ground-truth store, with per-tile caches."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.ais = AISLookup(conn)
        self._tiles: dict[str, sqlite3.Row | None] = {}
        self._dets: dict[str, list[sqlite3.Row]] = {}
        self._blind: dict[str, tuple[list[BlindResult], list[TrackPosition]]] = {}

    def tile(self, tile_id: str) -> sqlite3.Row | None:
        if tile_id not in self._tiles:
            self._tiles[tile_id] = self.conn.execute("SELECT * FROM optical_tiles WHERE tile_id = ?", (tile_id,)).fetchone()
        return self._tiles[tile_id]

    def detections(self, tile_id: str) -> list[sqlite3.Row]:
        if tile_id not in self._dets:
            self._dets[tile_id] = self.conn.execute(
                "SELECT * FROM optical_detections WHERE tile_id = ? ORDER BY shape_index", (tile_id,)).fetchall()
        return self._dets[tile_id]

    def blind(self, tile_id: str) -> tuple[list[BlindResult], list[TrackPosition]]:
        """Label-free association for the tile: the auditor never reads annotator MMSIs."""
        if tile_id not in self._blind:
            t = self.tile(tile_id)
            self._blind[tile_id] = associate_tile(t, self.detections(tile_id), self.ais) if t else ([], [])
        return self._blind[tile_id]

    def hulls_near(self, tile_id: str, lat: float, lon: float, radius_m: float) -> list[tuple[float, sqlite3.Row]]:
        near = [(haversine_m(lat, lon, d["center_lat"], d["center_lon"]), d) for d in self.detections(tile_id)]
        return sorted((x for x in near if x[0] <= radius_m), key=lambda x: x[0])


# ------------------------------------------------------------------- claims -----


def _claim(kind: str, tile_id: str, attrs: dict[str, Any], text: str, mmsi: int | None = None) -> Claim:
    subject = f"{tile_id}/mmsi:{mmsi}" if mmsi is not None else tile_id
    return Claim(id=f"{kind}:{subject}", kind=kind, subject=subject, attributes={"tile_id": tile_id, **attrs},
                 text=text, critical=kind in CRITICAL_KINDS)


def vessel_present(tile_id: str, mmsi: int, lat: float, lon: float) -> Claim:
    return _claim("vessel_present", tile_id, {"mmsi": mmsi, "lat": lat, "lon": lon},
                  f"MMSI {mmsi} is present at {lat:.5f}, {lon:.5f}", mmsi)


def vessel_count(tile_id: str, count: int) -> Claim:
    return _claim("vessel_count", tile_id, {"count": count}, f"{count} vessels are in the tile")


def dark_vessel_count(tile_id: str, count: int) -> Claim:
    text = "No dark vessels are in the tile" if count == 0 else f"{count} dark vessel(s) are in the tile"
    return _claim("dark_vessel_count", tile_id, {"count": count}, text)


def spoofing(tile_id: str, mmsi: int, lat: float, lon: float) -> Claim:
    return _claim("spoofing", tile_id, {"mmsi": mmsi, "lat": lat, "lon": lon},
                  f"MMSI {mmsi} is spoofing: its hull is at {lat:.5f}, {lon:.5f}, not where its AIS says", mmsi)


def going_dark(tile_id: str, mmsi: int) -> Claim:
    return _claim("going_dark", tile_id, {"mmsi": mmsi}, f"MMSI {mmsi} has gone dark (switched AIS off)", mmsi)


# -------------------------------------------------------------------- rules -----


def _identities(gt: MaritimeGroundTruth, c: Claim, t: sqlite3.Row) -> list[tuple[float, Verification]]:
    """Identity verdict of the claimed MMSI against each hull within PRESENCE_RADIUS_M."""
    a = c.attributes
    return [(d, classify_detection(a["mmsi"], h["center_lat"], h["center_lon"], h["length_m"], t["acquired_epoch"], gt.ais))
            for d, h in gt.hulls_near(a["tile_id"], a["lat"], a["lon"], PRESENCE_RADIUS_M)]


def _no_imagery(gt: MaritimeGroundTruth, c: Claim) -> tuple[Outcome, str] | None:
    a = c.attributes
    t = gt.tile(a["tile_id"])
    if t is None:
        return "unverifiable", f"no imagery for tile {a['tile_id']}"
    if "lat" in a and not inside_chip(t, a["lat"], a["lon"]):
        # Same edge inset as the ghost-AIS rule: a hull straddling the chip edge may be unannotated.
        return "unverifiable", "claimed position is outside (or on the edge of) the imaged footprint"
    return None


@rule("vessel_present")
def presence_in_imagery(c: Claim, gt: MaritimeGroundTruth) -> tuple[Outcome, str]:
    if (miss := _no_imagery(gt, c)) is not None:
        return miss
    t = gt.tile(c.attributes["tile_id"])
    checks = _identities(gt, c, t)
    if not checks:
        return "contradicted", f"satellite sees no hull within {PRESENCE_RADIUS_M:.0f} m of the claimed position"
    for d, v in checks:
        if v.verdict == "corroborated":
            return "supported", f"hull {d:.0f} m from the claim; identity corroborated ({v.reason})"
    d, v = checks[0]
    if v.verdict == "invalid_identity":
        return "contradicted", f"hull present, but {v.reason}: not a ship identity"
    if v.verdict == "position_discrepancy":
        return "unverifiable", f"hull present, but this MMSI's AIS is {(v.gap_m or 0) / 1000:.1f} km away: identity unconfirmed"
    return "unverifiable", f"hull present; identity unconfirmed: {v.reason}"


@rule("vessel_count")
def hull_count(c: Claim, gt: MaritimeGroundTruth) -> tuple[Outcome, str]:
    if (miss := _no_imagery(gt, c)) is not None:
        return miss
    n = len(gt.detections(c.attributes["tile_id"]))
    claimed = c.attributes["count"]
    return ("supported" if claimed == n else "contradicted"), f"satellite sees {n} hull(s); report says {claimed}"


@rule("dark_vessel_count")
def dark_count(c: Claim, gt: MaritimeGroundTruth) -> tuple[Outcome, str]:
    """Dark = blind-unmatched hulls >= 30 m. Unresolved hulls (another ship's AIS gap could
    explain them) widen the plausible range instead of being counted either way."""
    if (miss := _no_imagery(gt, c)) is not None:
        return miss
    results, _ = gt.blind(c.attributes["tile_id"])
    dark = sum(r.status == "dark_vessel" for r in results)
    unresolved = sum(r.status == "unresolved_gap" for r in results)
    claimed = c.attributes["count"]
    seen = f"blind association: {dark} dark-vessel candidate(s), {unresolved} unresolved"
    if not dark <= claimed <= dark + unresolved:
        return "contradicted", f"{seen}; report says {claimed}"
    if unresolved:
        return "unverifiable", f"{seen}; AIS gaps leave the true count between {dark} and {dark + unresolved}"
    return "supported", seen


@rule("spoofing")
def position_discrepancy(c: Claim, gt: MaritimeGroundTruth) -> tuple[Outcome, str]:
    if (miss := _no_imagery(gt, c)) is not None:
        return miss
    t = gt.tile(c.attributes["tile_id"])
    checks = _identities(gt, c, t)
    if not checks:
        return "contradicted", f"no hull within {PRESENCE_RADIUS_M:.0f} m of where the report places it"
    for _, v in checks:
        if v.verdict == "corroborated":
            return "contradicted", f"AIS agrees with the hull: {v.reason}"
    _, v = checks[0]
    if v.verdict == "position_discrepancy":
        return "supported", f"spoofing candidate for review: {v.reason}"
    return "unverifiable", f"can't compare positions: {v.reason}"


@rule("going_dark")
def ais_silence(c: Claim, gt: MaritimeGroundTruth) -> tuple[Outcome, str]:
    """Only the GFW-style rule (>= 12 h with good fleet reception) supports going dark;
    a shorter gap is insufficient data, never evidence of evasion."""
    a = c.attributes
    t = gt.tile(a["tile_id"])
    if t is None:
        return "unverifiable", f"no imagery for tile {a['tile_id']}"
    m = a["mmsi"]
    if (bad := validate_mmsi(m)) is not None:
        return "unverifiable", f"MMSI {m}: {bad}"
    if m not in gt.ais.registry:
        return "unverifiable", f"MMSI {m} is not in this AIS feed, so its silence can't be measured"
    p = gt.ais.position(m, t["acquired_epoch"])
    if p is not None:
        return "contradicted", f"AIS places it at image time ({p.method}, nearest fix {p.fix_age_s} s away)"
    v = assess_silence(m, t["acquired_epoch"], gt.ais)
    if v.verdict == "potential_going_dark":
        return "supported", f"going-dark candidate for review: {v.reason}"
    return "unverifiable", f"insufficient data, not evidence of evasion: {v.reason}"


RULES = [presence_in_imagery, hull_count, dark_count, position_discrepancy, ais_silence]


def claims_agree(a: Claim, b: Claim) -> bool:
    """Consistency pairing: same kind and subject; positions within 500 m, counts equal."""
    if a.kind != b.kind or a.subject != b.subject:
        return False
    x, y = a.attributes, b.attributes
    if a.kind in POSITIONAL_KINDS:
        return haversine_m(x["lat"], x["lon"], y["lat"], y["lon"]) <= SAMPLE_POSITION_AGREEMENT_M
    return x.get("count") == y.get("count")


def make_auditor() -> FaithfulnessAuditor:
    return FaithfulnessAuditor(RULES, agree=claims_agree)


def make_guardrail() -> Guardrail:
    return Guardrail(make_auditor())
