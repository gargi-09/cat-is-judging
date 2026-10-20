"""Reconcile satellite hulls with AIS: identity verification and label-free association.

Two modes, because they answer different questions:

* **Identity verification** — "this hull is MMSI X": is the claim consistent with AIS?
  This is what the auditor checks when an agent (or an annotator) asserts an identity.
* **Blind association** — the satellite gives unlabelled hulls; which AIS track, if any,
  belongs to each one? This is what a real product does. The dataset's annotator labels
  were themselves produced by matching to AIS, so trusting them at detection time would be
  circular; instead they are the answer key for scoring the association.

Every threshold below is named, documented and mirrored in the public sandbox (docs/).
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from applications.maritime_threat_detection.db.ais_repository import (
    TrackGap,
    TrackPosition,
    fleet_coverage,
    gap_at,
    position_across_gap,
    position_at,
)
from applications.maritime_threat_detection.geo import S2_TILE_GRID, haversine_m, latlon_to_pixel

# --- identity verification ---------------------------------------------------------
# Base corroboration radius. With interpolation to the image second, observed AIS-vs-hull
# residuals on real data are ~110 m median / ~225 m p90 (GPS antenna offset + annotation);
# 500 m is ~2x p90. Each estimate's own uncertainty is added on top, so stale or
# extrapolated fixes get proportionally more slack instead of one global radius. A blanket
# 2-5 km radius would have hidden a real 1.4 km discrepancy in the data.
CORROBORATION_BASE_M: float = 500.0
# Sentinel-2 is 10 m GSD: hulls under ~30 m are a few pixels, and small craft are not
# required to carry AIS. An unidentified hull this small is not a dark-vessel signal.
MIN_DARK_VESSEL_LENGTH_M: float = 30.0
# "No AIS near the image time" is only *potential going dark* when, following Global Fishing
# Watch's AIS-disabling definition, the silence lasts >= 12 h while reception was good (most
# of the fleet was still heard), and the ship moved across it or its track simply ends.
# On this data, 2-10 h gaps are reception gaps: the satellite sees those ships exactly on
# their AIS route. Shorter gaps are therefore "insufficient data", never a threat.
GOING_DARK_MIN_GAP_S: int = 12 * 3600
GOING_DARK_MIN_FLEET_COVERAGE: float = 0.5
GOING_DARK_MIN_DISPLACEMENT_M: float = 1000.0
GHOST_EDGE_INSET_PX: float = 5.0

# --- blind association ---------------------------------------------------------------
ASSOCIATION_SEARCH_BUFFER_M: float = 2000.0  # consider AIS tracks this far outside the chip
LENGTH_WEIGHT: float = 1.0   # relative length mismatch (0 = identical, 1 = 2x off)
HEADING_WEIGHT: float = 0.5  # long-axis vs AIS course mismatch, normalised by 90 deg
_UNASSIGNED_COST: float = 10.0  # any gated-in pair costs < 3, so matching always wins over this
_GATED_COST: float = 1e6

Verdict = Literal[
    "corroborated", "position_discrepancy", "ais_silent", "potential_going_dark",
    "dark_vessel", "small_craft", "invalid_identity", "unmatched_identity",
]
# Which verdicts are threat candidates (shown as alerts), and which are not.
THREAT_VERDICTS: frozenset[str] = frozenset(
    {"position_discrepancy", "potential_going_dark", "dark_vessel", "invalid_identity", "unmatched_identity"}
)


def validate_mmsi(mmsi: int) -> str | None:
    """Return why ``mmsi`` is not a plausible ship-station MMSI, or None if it is.

    Structural check per ITU-R M.585: ships use 9 digits beginning with a Maritime
    Identification Digit (MID) in 201-775. Other prefixes identify coast stations, SAR
    aircraft, AIS-SART/MOB/EPIRB beacons, aids to navigation or handhelds — a hull
    broadcasting one of those is misusing an identity. (This does not prove an MMSI is
    *assigned*; that needs the ITU MARS registry, which is not in this dataset.)
    """
    s = str(mmsi)
    if len(s) != 9:
        return f"{len(s)} digits (ship MMSIs have 9)"
    if s.startswith("00"):
        return "coast-station prefix 00"
    if s.startswith("0"):
        return "group-call prefix 0"
    if s.startswith("111"):
        return "SAR-aircraft prefix 111"
    if s[:3] in {"970", "972", "974"}:
        return f"emergency-beacon prefix {s[:3]} (AIS-SART / MOB / EPIRB)"
    if s.startswith("98"):
        return "craft-associated-with-parent prefix 98"
    if s.startswith("99"):
        return "aid-to-navigation prefix 99"
    if s.startswith("8"):
        return "handheld-VHF prefix 8"
    if not 201 <= int(s[:3]) <= 775:
        return f"MID {s[:3]} outside the ship range 201-775"
    return None


class AISLookup:
    """Cached AIS queries for one database connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        rows = conn.execute("SELECT mmsi, length_m, vessel_name FROM vessels").fetchall()
        self.registry: set[int] = {r[0] for r in rows}
        self.length_m: dict[int, float | None] = {r[0]: r[1] for r in rows}
        self.name: dict[int, str | None] = {r[0]: r[2] for r in rows}
        self._pos: dict[tuple[int, int], TrackPosition | None] = {}
        self._gap: dict[tuple[int, int], TrackGap] = {}
        self._bridge: dict[tuple[int, int], TrackPosition | None] = {}
        self._coverage: dict[tuple[int, int, int], float] = {}

    def position(self, mmsi: int, epoch: int) -> TrackPosition | None:
        key = (mmsi, epoch)
        if key not in self._pos:
            self._pos[key] = position_at(self.conn, mmsi, epoch)
        return self._pos[key]

    def gap(self, mmsi: int, epoch: int) -> TrackGap:
        key = (mmsi, epoch)
        if key not in self._gap:
            self._gap[key] = gap_at(self.conn, mmsi, epoch)
        return self._gap[key]

    def bridge(self, mmsi: int, epoch: int) -> TrackPosition | None:
        key = (mmsi, epoch)
        if key not in self._bridge:
            self._bridge[key] = position_across_gap(self.conn, mmsi, epoch)
        return self._bridge[key]

    def coverage(self, start: int, end: int, exclude: int) -> float:
        key = (start, end, exclude)
        if key not in self._coverage:
            self._coverage[key] = fleet_coverage(self.conn, start, end, exclude)
        return self._coverage[key]


class Verification(BaseModel):
    verdict: Verdict
    reason: str
    ais: TrackPosition | None = None
    gap_m: float | None = None
    tolerance_m: float | None = None

    @property
    def is_threat(self) -> bool:
        return self.verdict in THREAT_VERDICTS


def tolerance_m(p: TrackPosition) -> float:
    return CORROBORATION_BASE_M + p.uncertainty_m


def classify_detection(
    mmsi: int | None, lat: float, lon: float, length_m: float, epoch: int, ais: AISLookup
) -> Verification:
    """Verify a claimed identity for one hull seen at ``(lat, lon)`` at ``epoch``."""
    if mmsi is None:
        if length_m < MIN_DARK_VESSEL_LENGTH_M:
            return Verification(verdict="small_craft", reason=f"unidentified hull {length_m:.0f} m < {MIN_DARK_VESSEL_LENGTH_M:.0f} m; AIS not required")
        return Verification(verdict="dark_vessel", reason=f"unidentified hull {length_m:.0f} m long")
    bad = validate_mmsi(mmsi)
    if bad:
        return Verification(verdict="invalid_identity", reason=f"MMSI {mmsi}: {bad}")
    if mmsi not in ais.registry:
        return Verification(verdict="unmatched_identity", reason=f"MMSI {mmsi} never seen in this AIS feed")
    p = ais.position(mmsi, epoch)
    if p is None:
        return assess_silence(mmsi, epoch, ais, lat, lon)
    gap = haversine_m(lat, lon, p.lat, p.lon)
    tol = tolerance_m(p)
    verdict: Verdict = "corroborated" if gap <= tol else "position_discrepancy"
    return Verification(verdict=verdict, reason=f"{gap:,.0f} m from AIS ({p.method}), tolerance {tol:,.0f} m",
                        ais=p, gap_m=gap, tolerance_m=tol)


def assess_silence(mmsi: int, epoch: int, ais: AISLookup, lat: float | None = None, lon: float | None = None) -> Verification:
    """Why AIS can't place a registered ship at ``epoch``: going dark, or just a gap.

    With a hull position, also says whether the hull sits on the ship's AIS route.
    """
    g = ais.gap(mmsi, epoch)
    if g.gap_s is not None:
        start, end = g.prev_epoch or epoch, g.next_epoch or epoch
        long_gap, moved = g.gap_s >= GOING_DARK_MIN_GAP_S, (g.displacement_m or 0) >= GOING_DARK_MIN_DISPLACEMENT_M
        desc = f"AIS gap {g.gap_s / 3600:.1f} h, moved {(g.displacement_m or 0) / 1000:.1f} km across it"
    else:
        edge = g.prev_epoch if g.prev_epoch is not None else g.next_epoch
        start, end = sorted((edge if edge is not None else epoch, epoch))
        long_gap, moved = end - start >= GOING_DARK_MIN_GAP_S, True  # track ends: movement unknowable
        desc = f"track {'ends' if g.next_epoch is None else 'starts'} {(end - start) / 3600:.1f} h from the image"
    heard = ais.coverage(start, end, mmsi)
    if long_gap and moved and heard >= GOING_DARK_MIN_FLEET_COVERAGE:
        return Verification(verdict="potential_going_dark", reason=f"{desc} while {heard:.0%} of the fleet was heard")
    b = ais.bridge(mmsi, epoch)
    on_route = b is not None and lat is not None and lon is not None and haversine_m(lat, lon, b.lat, b.lon) <= tolerance_m(b)
    note = "hull lies on its AIS route: a reception gap" if on_route else "not a threat signal on its own"
    return Verification(verdict="ais_silent", reason=f"insufficient AIS ({desc}); {note}")


# ----------------------------------------------------------------- association -----


@dataclass(frozen=True)
class Hull:
    key: Any
    lat: float
    lon: float
    length_m: float
    axis_bearing_deg: float  # [0, 180)


@dataclass
class Assignment:
    hull_key: Any
    mmsi: int | None
    method: str | None = None  # AIS estimation method of the matched track
    distance_m: float | None = None
    tolerance_m: float | None = None
    cost: float | None = None
    terms: dict[str, float] = field(default_factory=dict)


def _axis_diff_deg(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def pair_cost(h: Hull, p: TrackPosition, ais_length_m: float | None) -> tuple[float, dict[str, float], float, float]:
    """Return ``(cost, terms, distance_m, tolerance_m)``; cost is gated when out of range."""
    d = haversine_m(h.lat, h.lon, p.lat, p.lon)
    tol = tolerance_m(p)
    terms = {"distance": (d / tol) ** 2}
    if ais_length_m:
        terms["length"] = LENGTH_WEIGHT * (abs(h.length_m - ais_length_m) / max(h.length_m, ais_length_m)) ** 2
    if p.course_deg is not None:
        terms["heading"] = HEADING_WEIGHT * (_axis_diff_deg(h.axis_bearing_deg, p.course_deg) / 90.0) ** 2
    return (sum(terms.values()) if d <= tol else _GATED_COST), terms, d, tol


def hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment for an n x m matrix with n <= m; returns column per row."""
    n, m = len(cost), len(cost[0]) if cost else 0
    if n == 0:
        return []
    inf = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    out = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def associate(hulls: list[Hull], candidates: list[TrackPosition], lengths: dict[int, float | None]) -> list[Assignment]:
    """Optimally match hulls to AIS tracks (one-to-one), leaving out-of-range ones unmatched.

    Cost = (distance / tolerance)^2 + length mismatch + heading mismatch; pairs beyond the
    tolerance are gated out. Each hull also gets a private "unassigned" column, so a hull is
    left unmatched rather than forced onto an implausible track.
    """
    if not hulls:
        return []
    details: dict[tuple[int, int], tuple[dict[str, float], float, float]] = {}
    matrix: list[list[float]] = []
    for i, h in enumerate(hulls):
        row = []
        for j, c in enumerate(candidates):
            cst, terms, d, tol = pair_cost(h, c, lengths.get(c.mmsi))
            details[(i, j)] = (terms, d, tol)
            row.append(cst)
        row += [_UNASSIGNED_COST if k == i else _GATED_COST for k in range(len(hulls))]
        matrix.append(row)
    cols = hungarian(matrix)
    out = []
    for i, j in enumerate(cols):
        if 0 <= j < len(candidates) and matrix[i][j] < _UNASSIGNED_COST:
            terms, d, tol = details[(i, j)]
            out.append(Assignment(hulls[i].key, candidates[j].mmsi, candidates[j].method, d, tol, matrix[i][j], terms))
        else:
            out.append(Assignment(hulls[i].key, None))
    return out


def tile_candidates(tile: sqlite3.Row, ais: AISLookup) -> list[TrackPosition]:
    """AIS estimates at acquisition time near one chip (footprint + search buffer)."""
    dlat = ASSOCIATION_SEARCH_BUFFER_M / 111_320.0
    dlon = dlat / math.cos(math.radians((tile["lat_min"] + tile["lat_max"]) / 2))
    out = []
    for m in sorted(ais.registry):
        p = ais.position(m, tile["acquired_epoch"])
        if p and tile["lat_min"] - dlat <= p.lat <= tile["lat_max"] + dlat and tile["lon_min"] - dlon <= p.lon <= tile["lon_max"] + dlon:
            out.append(p)
    return out


def inside_chip(tile: sqlite3.Row, lat: float, lon: float, inset_px: float = GHOST_EDGE_INSET_PX) -> bool:
    col, row = latlon_to_pixel(S2_TILE_GRID[tile["mgrs_tile"]], lat, lon)
    lx, ly = col - tile["px_x"], row - tile["px_y"]
    return inset_px <= lx <= tile["width_px"] - inset_px and inset_px <= ly <= tile["height_px"] - inset_px




BlindStatus = Literal["matched", "unresolved_gap", "dark_vessel", "small_craft"]


@dataclass
class BlindResult:
    assignment: Assignment
    status: BlindStatus
    near_gap_mmsis: list[int] = field(default_factory=list)


def associate_tile(tile: sqlite3.Row, detections: list[sqlite3.Row], ais: AISLookup) -> tuple[list[BlindResult], list[TrackPosition]]:
    """Blind-associate one chip. Returns (result per detection, ghost-AIS candidates).

    An unmatched hull is only a dark-vessel candidate if no ship with an AIS reception gap
    spanning the image could plausibly be it. Gap routes are NOT used to assign identity:
    on this data, straight-line routes across multi-hour gaps picked the right ship only
    ~11% of the time in dense anchorages. They are only used to withhold a false alarm.
    """
    hulls = [Hull(d["id"], d["center_lat"], d["center_lon"], d["length_m"], d["axis_bearing_deg"]) for d in detections]
    cands = tile_candidates(tile, ais)
    assignments = associate(hulls, cands, ais.length_m)
    taken = {a.mmsi for a in assignments if a.mmsi is not None}
    ghosts = [c for c in cands if c.mmsi not in taken and inside_chip(tile, c.lat, c.lon)]

    unplaced = sorted(ais.registry - {c.mmsi for c in cands})
    bridges = [b for m in unplaced if (b := ais.bridge(m, tile["acquired_epoch"])) is not None]
    out = []
    for h, a in zip(hulls, assignments):
        if a.mmsi is not None:
            out.append(BlindResult(a, "matched"))
            continue
        near = [b.mmsi for b in bridges if haversine_m(h.lat, h.lon, b.lat, b.lon) <= tolerance_m(b)]
        if near:
            out.append(BlindResult(a, "unresolved_gap", near))
        else:
            out.append(BlindResult(a, "dark_vessel" if h.length_m >= MIN_DARK_VESSEL_LENGTH_M else "small_craft"))
    return out, ghosts


# ----------------------------------------------------------------- full report -----


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def cross_reference(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run both modes over every chip and score blind association against the labels."""
    ais = AISLookup(conn)
    tiles = conn.execute("SELECT * FROM optical_tiles ORDER BY tile_id").fetchall()
    dets = conn.execute("SELECT * FROM optical_detections ORDER BY id").fetchall()
    by_tile: dict[str, list[sqlite3.Row]] = {}
    for d in dets:
        by_tile.setdefault(d["tile_id"], []).append(d)
    epoch_of = {t["tile_id"]: t["acquired_epoch"] for t in tiles}

    # Identity verification: the annotator's MMSI is the claim under test.
    verdicts: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    residuals: list[float] = []
    discrepancies: list[dict[str, Any]] = []
    for d in dets:
        v = classify_detection(d["mmsi"], d["center_lat"], d["center_lon"], d["length_m"], epoch_of[d["tile_id"]], ais)
        verdicts[v.verdict] += 1
        if v.ais is not None and v.gap_m is not None:
            methods[v.ais.method] += 1
            residuals.append(v.gap_m)
        if v.verdict == "position_discrepancy":
            discrepancies.append({"tile_id": d["tile_id"], "mmsi": d["mmsi"], "residual_m": round(v.gap_m or 0),
                                  "tolerance_m": round(v.tolerance_m or 0)})

    # Blind association, scored against the labels (the answer key).
    status: Counter[str] = Counter()
    outcome: Counter[str] = Counter()
    ghosts: list[dict[str, Any]] = []
    wrong: list[dict[str, Any]] = []
    unresolved_has_truth = 0
    for t in tiles:
        tdets = by_tile.get(t["tile_id"], [])
        results, tghosts = associate_tile(t, tdets, ais)
        label = {d["id"]: d["mmsi"] for d in tdets}
        for r in results:
            truth = label[r.assignment.hull_key]
            status[r.status] += 1
            if r.status == "matched":
                ok = r.assignment.mmsi == truth
                outcome["correct" if ok else "wrong"] += 1
                if not ok:
                    wrong.append({"tile_id": t["tile_id"], "label": truth, "assigned": r.assignment.mmsi,
                                  "distance_m": round(r.assignment.distance_m or 0)})
            elif r.status == "unresolved_gap" and truth in r.near_gap_mmsis:
                unresolved_has_truth += 1
        ghosts += [{"tile_id": t["tile_id"], "mmsi": g.mmsi, "method": g.method} for g in tghosts]

    placeable = sum(1 for d in dets if d["mmsi"] in ais.registry and ais.position(d["mmsi"], epoch_of[d["tile_id"]]))
    matched = outcome["correct"] + outcome["wrong"]
    return {
        "detections_total": len(dets),
        "identity_verification": {
            "verdicts": dict(sorted(verdicts.items())),
            "threat_candidates": sum(n for k, n in verdicts.items() if k in THREAT_VERDICTS),
            "ais_position_method": dict(sorted(methods.items())),
            "residual_m": (
                {"median": round(statistics.median(residuals), 1), "p90": round(_pct(residuals, 0.9), 1),
                 "p99": round(_pct(residuals, 0.99), 1), "max": round(max(residuals), 1)} if residuals else None
            ),
            "position_discrepancies": sorted(discrepancies, key=lambda x: -x["residual_m"]),
        },
        "blind_association": {
            "status": dict(sorted(status.items())),
            "matched_correct": outcome["correct"],
            "matched_wrong": outcome["wrong"],
            "precision": round(outcome["correct"] / matched, 4) if matched else None,
            "recall_placeable": round(outcome["correct"] / placeable, 4) if placeable else None,
            "recall_all": round(outcome["correct"] / len(dets), 4) if dets else None,
            "placeable_detections": placeable,
            "unresolved_gap_contains_true_ship": unresolved_has_truth,
            "threat_candidates": {"dark_vessel": status["dark_vessel"], "ghost_ais": len(ghosts),
                                  "ghost_ais_vessels": len({g["mmsi"] for g in ghosts})},
            # Every label in this dataset is a real AIS vessel, so each blind "dark vessel"
            # is by construction a false alarm — reported, not hidden.
            "false_dark_rate": round(status["dark_vessel"] / len(dets), 4) if dets else None,
            "wrong_assignments": wrong[:25],
            "ghost_ais_examples": ghosts[:10],
        },
    }
