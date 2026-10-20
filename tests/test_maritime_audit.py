"""Maritime rules for the auditor, on a synthetic chip with one ship of each kind."""

from __future__ import annotations

import math
import sqlite3

import pytest

from applications.maritime_threat_detection import audit as A
from applications.maritime_threat_detection.analyst_stub import draft_report
from applications.maritime_threat_detection.db import apply_schema
from applications.maritime_threat_detection.geo import EARTH_RADIUS_M, S2_TILE_GRID, pixel_to_latlon

H = 3600
T = 10 * H  # image time
TILE = "T40RDN_20260303_x3000_y1000"
GRID = S2_TILE_GRID["40RDN"]
X0, Y0, SIZE = 3000, 1000, 500  # chip: 500 px = 5 km


def ll(x: float, y: float) -> tuple[float, float]:
    return pixel_to_latlon(GRID, X0 + x, Y0 + y)


def north_of(p: tuple[float, float], m: float) -> tuple[float, float]:
    return p[0] + m / (math.radians(1) * EARTH_RADIUS_M), p[1]


HULL_A = ll(100, 100)  # 538000001, anchored and reporting: corroborated
HULL_B = ll(300, 300)  # 538000002's real hull; its AIS says AIS_B (spoofing candidate)
AIS_B = ll(400, 100)
HULL_C = ll(200, 400)  # 538000003, 3 h reception gap, hull on its route
HULL_D = ll(450, 50)   # unidentified 80 m hull, no AIS anywhere near: dark vessel
FAR = ll(5000, 5000)   # 538000004 goes silent for 13 h well outside the chip


@pytest.fixture()
def gt() -> A.MaritimeGroundTruth:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    conn.executemany("INSERT INTO vessels (mmsi, length_m) VALUES (?, ?)",
                     [(538000001, 180.0), (538000002, 200.0), (538000003, 150.0), (538000004, 220.0)])
    fixes = [(538000001, h * H, *HULL_A, 0.0) for h in range(21)] + [(538000002, h * H, *AIS_B, 0.0) for h in range(21)]
    fixes += [(538000003, 9 * H, *north_of(HULL_C, -1000), 5.0), (538000003, 12 * H, *north_of(HULL_C, 2000), 5.0)]
    fixes += [(538000004, 2 * H, *FAR, 8.0), (538000004, 15 * H, *north_of(FAR, 20_000), 8.0)]
    conn.executemany("INSERT INTO ais_positions (mmsi, postime, postime_epoch, lat, lon, sog) VALUES (?, '', ?, ?, ?, ?)", fixes)

    corners = [ll(0, 0), ll(SIZE, 0), ll(0, SIZE), ll(SIZE, SIZE)]
    lats, lons = [c[0] for c in corners], [c[1] for c in corners]
    conn.execute(
        "INSERT INTO optical_tiles VALUES (?, 'S2', '40RDN', '', ?, ?, ?, ?, ?, 10, ?, ?, ?, ?, 4, '', NULL)",
        (TILE, T, X0, Y0, SIZE, SIZE, min(lats), max(lats), min(lons), max(lons)))
    hulls = [(538000001, HULL_A, 180), (538000002, HULL_B, 200), (538000003, HULL_C, 150), (None, HULL_D, 80)]
    conn.executemany(
        "INSERT INTO optical_detections (tile_id, shape_index, mmsi, center_lat, center_lon, center_px_x, center_px_y, "
        "length_m, width_m, axis_bearing_deg, shape_type, polygon_px) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 20, 0, 'rotation', '[]')",
        [(TILE, i, m, *p, length) for i, (m, p, length) in enumerate(hulls)])
    return A.MaritimeGroundTruth(conn)


def outcome(gt: A.MaritimeGroundTruth, claim) -> str:
    return A.make_auditor().verifier.verify(claim, gt).outcome


def test_presence_is_decided_by_the_imagery(gt: A.MaritimeGroundTruth) -> None:
    assert outcome(gt, A.vessel_present(TILE, 538000001, *HULL_A)) == "supported"
    # Where the spoofer's AIS says it is, the satellite sees nothing: a ghost.
    assert outcome(gt, A.vessel_present(TILE, 538000002, *AIS_B)) == "contradicted"
    # At its real hull, presence is fine but identity can't be confirmed from AIS.
    assert outcome(gt, A.vessel_present(TILE, 538000002, *HULL_B)) == "unverifiable"
    assert outcome(gt, A.vessel_present(TILE, 538000003, *HULL_C)) == "unverifiable"  # reception gap
    assert outcome(gt, A.vessel_present(TILE, 970123456, *HULL_D)) == "contradicted"  # beacon MMSI on a hull
    assert outcome(gt, A.vessel_present(TILE, 538000001, *FAR)) == "unverifiable"  # outside the footprint


def test_counts(gt: A.MaritimeGroundTruth) -> None:
    assert outcome(gt, A.vessel_count(TILE, 4)) == "supported"
    assert outcome(gt, A.vessel_count(TILE, 2)) == "contradicted"
    # Blind: hulls B and D are dark candidates, C is unresolved (538000003's gap route).
    assert outcome(gt, A.dark_vessel_count(TILE, 0)) == "contradicted"
    assert outcome(gt, A.dark_vessel_count(TILE, 2)) == "unverifiable"
    assert outcome(gt, A.dark_vessel_count(TILE, 3)) == "unverifiable"
    assert outcome(gt, A.dark_vessel_count(TILE, 4)) == "contradicted"


def test_accusations(gt: A.MaritimeGroundTruth) -> None:
    assert outcome(gt, A.spoofing(TILE, 538000002, *HULL_B)) == "supported"
    assert outcome(gt, A.spoofing(TILE, 538000001, *HULL_A)) == "contradicted"
    assert outcome(gt, A.going_dark(TILE, 538000004)) == "supported"     # 13 h, moved, fleet heard
    assert outcome(gt, A.going_dark(TILE, 538000003)) == "unverifiable"  # 3 h: insufficient data
    assert outcome(gt, A.going_dark(TILE, 538000001)) == "contradicted"  # AIS places it
    assert A.going_dark(TILE, 538000004).critical and not A.vessel_present(TILE, 1, 0, 0).critical


def test_claims_agree_within_the_presence_radius() -> None:
    a = A.vessel_present(TILE, 538000001, *HULL_A)
    assert A.claims_agree(a, A.vessel_present(TILE, 538000001, *north_of(HULL_A, 300)))
    assert not A.claims_agree(a, A.vessel_present(TILE, 538000001, *north_of(HULL_A, 800)))
    assert not A.claims_agree(A.vessel_count(TILE, 3), A.vessel_count(TILE, 4))


def test_ais_trusting_report_is_blocked(gt: A.MaritimeGroundTruth) -> None:
    d = A.make_guardrail().intercept(lambda i: draft_report(gt, TILE, "ais_trusting", i), gt, 5)
    assert d.action == "block" and d.released is None
    bad = {c.claim.kind for c in d.audit.hallucinations}
    assert {"vessel_present", "dark_vessel_count"} <= bad  # the ghost position, and "no dark vessels"


def test_imagery_first_report_is_amended_not_blocked(gt: A.MaritimeGroundTruth) -> None:
    d = A.make_guardrail().intercept(lambda i: draft_report(gt, TILE, "imagery_first", i), gt, 5)
    assert d.action == "amend" and not d.removed
    assert [c.kind for c in d.hedged] == ["dark_vessel_count"]  # AIS gaps leave the count open
    assert d.amended_audit is not None and d.amended_audit.passed


def test_alarmist_accusation_on_a_short_gap_is_not_supported(gt: A.MaritimeGroundTruth) -> None:
    r = draft_report(gt, TILE, "alarmist")
    gd = [c for c in A.make_auditor().audit(r, gt).claims if c.claim.kind == "going_dark"]
    assert [(c.claim.attributes["mmsi"], c.outcome) for c in gd] == [(538000003, "unverifiable")]


def test_auditor_never_reads_annotator_labels(gt: A.MaritimeGroundTruth) -> None:
    before = draft_report(gt, TILE, "imagery_first")
    gt.conn.execute("UPDATE optical_detections SET mmsi = NULL")
    fresh = A.MaritimeGroundTruth(gt.conn)
    assert draft_report(fresh, TILE, "imagery_first") == before
    a1, a2 = A.make_auditor().audit(before, gt), A.make_auditor().audit(before, fresh)
    assert [c.outcome for c in a1.claims] == [c.outcome for c in a2.claims]
