"""Threat rules, uncertainty-aware AIS lookups and label-free association."""

from __future__ import annotations

import math
import sqlite3
from itertools import permutations

import pytest

from applications.maritime_threat_detection.db import apply_schema
from applications.maritime_threat_detection.db.ais_repository import (
    KNOT_MPS,
    TrackPosition,
    position_across_gap,
    position_at,
)
from applications.maritime_threat_detection.geo import EARTH_RADIUS_M, haversine_m
from applications.maritime_threat_detection.reconciliation import (
    CORROBORATION_BASE_M,
    AISLookup,
    Hull,
    associate,
    classify_detection,
    hungarian,
    validate_mmsi,
)

LAT, LON = 25.2, 56.5
M_PER_DEG = math.radians(1) * EARTH_RADIUS_M
H = 3600


def _north(m: float) -> float:
    return LAT + m / M_PER_DEG


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    vessels = {
        538000001: 180.0,  # anchored, reporting normally
        538000002: 200.0,  # underway, single stale fix with course -> dead reckoning
        538000003: 150.0,  # underway, single fix, no course -> nearest + drift
        538000004: 220.0,  # 13 h gap, moved 20 km, fleet heard -> potential going dark
        538000005: 120.0,  # 3 h gap, moved 20 km -> reception gap, not a threat
    }
    conn.executemany("INSERT INTO vessels (mmsi, length_m) VALUES (?, ?)", vessels.items())
    fixes = [
        (538000001, 0, LAT, LON, 0.0, None), (538000001, 600, LAT, LON, 0.0, None),
        (538000001, 20 * H, LAT, LON, 0.0, None),
        (538000002, 10 * H - 1200, LAT, LON, 10.0, 0.0),  # 10 kn due north, 20 min before t
        (538000003, 10 * H - 600, LAT, LON, 10.0, None),   # 10 kn, course unknown
        (538000004, 2 * H, LAT, LON, 8.0, 90.0), (538000004, 15 * H, _north(20_000), LON, 8.0, 0.0),
        (538000005, 9 * H, LAT, LON, 8.0, 90.0), (538000005, 12 * H, _north(20_000), LON, 8.0, 0.0),
    ]
    # The rest of the fleet keeps reporting hourly (good reception).
    fixes += [(538000001, t * H, LAT, LON, 0.0, None) for t in range(2, 20)]
    conn.executemany(
        "INSERT INTO ais_positions (mmsi, postime, postime_epoch, lat, lon, sog, cog) VALUES (?, '', ?, ?, ?, ?, ?)",
        fixes,
    )
    return conn


# ---------------------------------------------------------------- AIS lookups ----


def test_dead_reckoning_from_single_stale_fix(db: sqlite3.Connection) -> None:
    p = position_at(db, 538000002, 10 * H)
    assert p is not None and p.method == "dead_reckoned" and p.fix_age_s == 1200
    expected_north = 10 * KNOT_MPS * 1200
    assert haversine_m(LAT, LON, p.lat, p.lon) == pytest.approx(expected_north, rel=1e-3)
    assert p.uncertainty_m == pytest.approx(0.2 * expected_north, rel=1e-3)


def test_nearest_without_course_carries_drift_uncertainty(db: sqlite3.Connection) -> None:
    p = position_at(db, 538000003, 10 * H)
    assert p is not None and p.method == "nearest"
    assert p.uncertainty_m == pytest.approx(10 * KNOT_MPS * 600, rel=1e-3)  # it could be 3 km away


def test_gap_bridge_is_a_wide_corridor(db: sqlite3.Connection) -> None:
    t = int(10.5 * H)
    assert position_at(db, 538000005, t) is None
    b = position_across_gap(db, 538000005, t)
    assert b is not None and b.method == "gap_bridged" and b.uncertainty_m == pytest.approx(5000, rel=1e-2)
    assert position_across_gap(db, 538000004, 8 * H) is None  # 13 h gap: beyond the 12 h cap


# --------------------------------------------------------------- MMSI checks -----


@pytest.mark.parametrize(
    "mmsi, ok",
    [(538012301, True), (201000000, True), (775999999, True), (12345, False), (970123456, False),
     (992351000, False), (111232506, False), (2350123, False), (800123456, False), (199123456, False),
     (776123456, False)],
)
def test_validate_mmsi(mmsi: int, ok: bool) -> None:
    assert (validate_mmsi(mmsi) is None) is ok


# -------------------------------------------------------------- verdict rules ----


def test_unidentified_hull_needs_size_to_be_dark(db: sqlite3.Connection) -> None:
    ais = AISLookup(db)
    assert classify_detection(None, LAT, LON, 25.0, 0, ais).verdict == "small_craft"
    v = classify_detection(None, LAT, LON, 80.0, 0, ais)
    assert v.verdict == "dark_vessel" and v.is_threat


def test_identity_checks(db: sqlite3.Connection) -> None:
    ais = AISLookup(db)
    assert classify_detection(970123456, LAT, LON, 100, 0, ais).verdict == "invalid_identity"
    assert classify_detection(636999999, LAT, LON, 100, 0, ais).verdict == "unmatched_identity"


def test_corroboration_tolerance_grows_with_uncertainty(db: sqlite3.Connection) -> None:
    ais = AISLookup(db)
    # Hull 2 km north of the stale no-course fix: within base + drift (~3.1 km) -> corroborated.
    v = classify_detection(538000003, _north(2000), LON, 150, 10 * H, ais)
    assert v.verdict == "corroborated" and v.tolerance_m and v.tolerance_m > CORROBORATION_BASE_M + 3000
    # The same 2 km offset on a well-placed anchored ship is a discrepancy.
    assert classify_detection(538000001, _north(2000), LON, 180, 300, ais).verdict == "position_discrepancy"


def test_long_gap_with_good_reception_is_potential_going_dark(db: sqlite3.Connection) -> None:
    v = classify_detection(538000004, _north(9000), LON, 220, 8 * H, AISLookup(db))
    assert v.verdict == "potential_going_dark" and v.is_threat


def test_short_gap_is_insufficient_data_not_a_threat(db: sqlite3.Connection) -> None:
    v = classify_detection(538000005, _north(10_000), LON, 120, int(10.5 * H), AISLookup(db))
    assert v.verdict == "ais_silent" and not v.is_threat
    assert "reception gap" in v.reason  # the hull sits on its AIS route


# --------------------------------------------------------------- association -----


def test_hungarian_matches_brute_force() -> None:
    cost = [[4, 1, 3, 9], [2, 0, 5, 7], [3, 2, 2, 1]]
    best = min(permutations(range(4), 3), key=lambda p: sum(cost[i][j] for i, j in enumerate(p)))
    got = hungarian(cost)
    assert sum(cost[i][j] for i, j in enumerate(got)) == sum(cost[i][j] for i, j in enumerate(best))


def _track(mmsi: int, north_m: float, course: float | None = None, unc: float = 0.0) -> TrackPosition:
    return TrackPosition(mmsi=mmsi, epoch=0, lat=_north(north_m), lon=LON, method="interpolated",
                         fix_age_s=0, uncertainty_m=unc, course_deg=course)


def test_associate_is_one_to_one_and_gated() -> None:
    hulls = [Hull("a", _north(0), LON, 200, 0), Hull("b", _north(300), LON, 200, 0), Hull("far", _north(5000), LON, 200, 0)]
    tracks = [_track(1, 50), _track(2, 320)]
    out = {a.hull_key: a.mmsi for a in associate(hulls, tracks, {1: 200, 2: 200})}
    assert out == {"a": 1, "b": 2, "far": None}  # far hull is left unmatched, not forced


def test_associate_uses_length_and_heading_to_break_ties() -> None:
    # Two tracks equidistant from two hulls; only length + heading tell them apart.
    hulls = [Hull("big", _north(0), LON, 300, 90), Hull("small", _north(100), LON, 90, 0)]
    tracks = [_track(1, 50, course=0), _track(2, 50, course=90)]
    out = {a.hull_key: a.mmsi for a in associate(hulls, tracks, {1: 90, 2: 300})}
    assert out == {"big": 2, "small": 1}
