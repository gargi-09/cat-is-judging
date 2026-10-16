"""Phase 1: deterministic ingestion & georeferencing for the maritime application."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from applications.maritime_threat_detection import DB_PATH
from applications.maritime_threat_detection.db import apply_schema
from applications.maritime_threat_detection.db.ais_repository import position_at
from applications.maritime_threat_detection.geo import (
    S2_TILE_GRID,
    haversine_m,
    latlon_to_pixel,
    latlon_to_utm,
    pixel_to_latlon,
    utm_to_latlon,
)
from applications.maritime_threat_detection.scripts.ingest_telemetry import REQUIRED_COLUMNS, clean_ais
from applications.maritime_threat_detection.tools.vision_tools import (
    load_tile,
    normalize_mmsi_label,
    parse_tile_name,
)

STEM = "S2B_MSIL2A_20260303T064659_N0512_R020_T40RDN_20260303T105730.SAFE_png__x3416_y1315"


# ---------------------------------------------------------------- geodesy ----


@pytest.mark.parametrize("lat, lon", [(25.1, 56.6), (24.4, 56.3), (26.0, 57.8), (0.5, 57.0)])
def test_utm_round_trip_is_submetre(lat: float, lon: float) -> None:
    e, n = latlon_to_utm(lat, lon, 40)
    lat2, lon2 = utm_to_latlon(e, n, 40)
    assert haversine_m(lat, lon, lat2, lon2) < 0.01


def test_utm_known_point() -> None:
    # Central meridian of zone 40 on the equator maps to the false easting exactly.
    e, n = latlon_to_utm(0.0, 57.0, 40)
    assert e == pytest.approx(500_000.0, abs=1e-6)
    assert n == pytest.approx(0.0, abs=1e-6)


def test_pixel_round_trip() -> None:
    grid = S2_TILE_GRID["40RDN"]
    lat, lon = pixel_to_latlon(grid, 3416.5, 1315.25)
    col, row = latlon_to_pixel(grid, lat, lon)
    assert (col, row) == pytest.approx((3416.5, 1315.25), abs=1e-4)


# ------------------------------------------------------------ annotations ----


@pytest.mark.parametrize(
    "label, expected",
    [
        ("538012301.0", 538012301),
        ("538012301", 538012301),
        (538012301.0, 538012301),
        ("0", None),
        ("0.0", None),
        ("unknown", None),
        ("Unknown ", None),
        ("", None),
        (None, None),
        ("5380.5", None),  # non-integral: refuse, never truncate into a real MMSI
        ("-1", None),
        ("nan", None),
    ],
)
def test_normalize_mmsi_label(label: object, expected: int | None) -> None:
    assert normalize_mmsi_label(label) == expected


def test_parse_tile_name() -> None:
    product, mgrs, sensing, x, y = parse_tile_name(STEM)
    assert product.startswith("S2B_MSIL2A_20260303T064659")
    assert (mgrs, x, y) == ("40RDN", 3416, 1315)
    assert sensing == datetime(2026, 3, 3, 6, 46, 59, tzinfo=timezone.utc)


def test_parse_tile_name_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_tile_name("not_a_sentinel_chip")


def test_load_tile_geometry(tmp_path: Path) -> None:
    # 20 px x 4 px box with its long axis due east (image +x) -> 200 m x 40 m, bearing 90.
    payload = {
        "imageWidth": 256,
        "imageHeight": 256,
        "shapes": [
            {"label": "538012301.0", "shape_type": "rotation", "points": [[100, 100], [120, 100], [120, 104], [100, 104]]},
            {"label": "unknown", "shape_type": "rotation", "points": [[10, 10], [10, 40], [14, 40], [14, 10]]},
            {"label": "1.0", "shape_type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
        ],
    }
    path = tmp_path / f"{STEM}.json"
    path.write_text(json.dumps(payload))
    tile = load_tile(path)

    assert tile.tile_id == "T40RDN_20260303_x3416_y1315"
    assert tile.skipped_shapes == 1  # the triangle is counted, not guessed
    ship, dark = tile.detections
    assert ship.mmsi == 538012301 and dark.mmsi is None
    assert (ship.length_m, ship.width_m) == pytest.approx((200.0, 40.0))
    assert ship.axis_bearing_deg == pytest.approx(90.0)
    assert dark.axis_bearing_deg == pytest.approx(0.0)  # long axis north-south
    assert tile.lat_min < ship.center_lat < tile.lat_max
    assert tile.lon_min < ship.center_lon < tile.lon_max
    # Chip is 256 px * 10 m = 2.56 km on a side.
    assert haversine_m(tile.lat_min, tile.lon_min, tile.lat_min, tile.lon_max) == pytest.approx(2560, rel=0.01)


# -------------------------------------------------------------------- ETL ----


def _raw_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {c: None for c in REQUIRED_COLUMNS}
    row.update(
        mmsi=538012301, postime="2026-03-03 06:47:00", lat=25.1, lon=56.6, rot=0.0, sog=0.1, cog=95.0,
        hdg=321.0, status=1, imo=9299472, callsign="v4hj6 ", vessel_name="KAISER", vesseltype=70,
        a=153, b=39, c=8, d=16, length=192, width=24.0, draught=6.1, dest="OM SOH",
        eta="2026-02-28 06:00:00+08", flag_ctry=None,
    )
    row.update(overrides)
    return row


def test_clean_ais_normalises_and_drops_with_reasons() -> None:
    raw = pd.DataFrame(
        [
            _raw_row(),
            _raw_row(postime="2026-03-03 06:48:00", hdg=511, cog=360.0, sog=102.3, rot=-128, imo=0, width=None),
            _raw_row(postime="2026-03-03 06:48:00"),  # duplicate (mmsi, postime): last wins
            _raw_row(mmsi=12345, postime="2026-03-03 06:49:00"),  # invalid MMSI
            _raw_row(postime="2026-03-03 06:50:00", lat=0.0, lon=0.0),  # null island
            _raw_row(postime="garbage"),
        ]
    )
    positions, vessels, dropped, conflicts = clean_ais(raw, timedelta(0))

    assert dict(dropped) == {"invalid_mmsi": 1, "invalid_postime": 1, "invalid_position": 1, "duplicate_mmsi_postime": 1}
    assert len(positions) == 2
    first = positions.iloc[0]
    assert first["postime"] == "2026-03-03T06:47:00Z"
    assert first["postime_epoch"] == int(datetime(2026, 3, 3, 6, 47, tzinfo=timezone.utc).timestamp())
    assert first["eta"] == "2026-02-27T22:00:00Z"  # +08 offset honoured
    assert len(vessels) == 1 and conflicts == 0
    v = vessels.iloc[0]
    assert v["callsign"] == "V4HJ6" and v["imo"] == 9299472 and v["length_m"] == 192


def test_clean_ais_sentinels_become_null() -> None:
    raw = pd.DataFrame([_raw_row(hdg=511, cog=360.0, sog=102.3, rot=-128)])
    positions, *_ = clean_ais(raw, timedelta(0))
    row = positions.iloc[0]
    assert all(pd.isna(row[c]) for c in ("hdg", "cog", "sog", "rot"))


def test_clean_ais_applies_utc_offset() -> None:
    positions, *_ = clean_ais(pd.DataFrame([_raw_row(postime="2026-03-03 14:47:00")]), timedelta(hours=8))
    assert positions.iloc[0]["postime"] == "2026-03-03T06:47:00Z"


def test_clean_ais_derives_width_from_antenna_offsets() -> None:
    _, vessels, *_ = clean_ais(pd.DataFrame([_raw_row(width=None, c=8, d=16)]), timedelta(0))
    assert vessels.iloc[0]["width_m"] == 24


# ------------------------------------------------------------- repository ----


@pytest.fixture()
def track_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    conn.execute("INSERT INTO vessels (mmsi) VALUES (1), (2), (3)")
    fixes = [
        (1, 1000, 25.0, 56.0), (1, 1600, 25.01, 56.0),          # moving, 10 min apart
        (2, 0, 25.2, 56.2), (2, 6 * 3600, 25.2005, 56.2),        # anchored, 6 h de-dup gap
        (3, 0, 25.3, 56.3), (3, 6 * 3600, 25.4, 56.3),           # moved 11 km across 6 h gap
    ]
    conn.executemany(
        "INSERT INTO ais_positions (mmsi, postime, postime_epoch, lat, lon) VALUES (?, '', ?, ?, ?)",
        [(m, t, la, lo) for m, t, la, lo in fixes],
    )
    return conn


def test_position_at_interpolates(track_db: sqlite3.Connection) -> None:
    p = position_at(track_db, 1, 1300)
    assert p is not None and p.method == "interpolated" and p.fix_age_s == 300
    assert p.lat == pytest.approx(25.005)


def test_position_at_exact_and_nearest(track_db: sqlite3.Connection) -> None:
    assert position_at(track_db, 1, 1000).method == "exact"  # type: ignore[union-attr]
    nearest = position_at(track_db, 1, 1600 + 600)
    assert nearest is not None and nearest.method == "nearest" and nearest.fix_age_s == 600
    assert position_at(track_db, 1, 1600 + 3600) is None  # AIS genuinely silent


def test_position_at_stationary_hold_only_when_stationary(track_db: sqlite3.Connection) -> None:
    held = position_at(track_db, 2, 3 * 3600)
    assert held is not None and held.method == "stationary_hold"
    # A vessel that moved 11 km across the gap must not be placed anywhere.
    assert position_at(track_db, 3, 3 * 3600) is None


# ------------------------------------------------------ built DB (optional) ----


@pytest.mark.skipif(not DB_PATH.exists(), reason="needs the raw dataset (not distributed); runs only where maritime.db has been built")
def test_built_database_invariants() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    n_tiles, n_det = conn.execute("SELECT COUNT(*), SUM(n_detections) FROM optical_tiles").fetchone()
    assert n_det == conn.execute("SELECT COUNT(*) FROM optical_detections").fetchone()[0]
    # Every detection must fall inside its own chip footprint.
    outside = conn.execute(
        "SELECT COUNT(*) FROM optical_detections d JOIN optical_tiles t USING (tile_id) "
        "WHERE d.center_lat NOT BETWEEN t.lat_min AND t.lat_max OR d.center_lon NOT BETWEEN t.lon_min AND t.lon_max"
    ).fetchone()[0]
    assert outside == 0
    assert conn.execute("SELECT COUNT(*) FROM ais_positions WHERE postime NOT LIKE '____-__-__T__:__:__Z'").fetchone()[0] == 0
