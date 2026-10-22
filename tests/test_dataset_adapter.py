"""Dataset adapter: MGRS origins, manifests, column mapping, georeferenced imagery, clock sweep."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from applications.maritime_threat_detection.dataset import (
    AIS_OPTIONAL,
    AIS_REQUIRED,
    apply_mapping,
    load_manifest,
    read_ais,
)
from applications.maritime_threat_detection.geo import (
    S2_DERIVED_ORIGIN_UNCERTAINTY_M,
    S2_TILE_GRID,
    latlon_to_utm,
    mgrs_square_sw,
    parse_tile_metadata,
    s2_grid,
    utm_to_latlon,
)
from applications.maritime_threat_detection.reconciliation import inside_chip
from applications.maritime_threat_detection.scripts.ingest_telemetry import run_manifest

# ------------------------------------------------------------------ geodesy -----


@pytest.mark.parametrize("mgrs", sorted(S2_TILE_GRID))
def test_derived_origin_is_close_to_the_registered_one(mgrs: str) -> None:
    exact, derived = S2_TILE_GRID[mgrs], s2_grid(mgrs, origins={})
    assert derived.origin_source == "registered"  # the registry wins over derivation
    zone, south, e, n = mgrs_square_sw(mgrs)
    assert (zone, south) == (exact.utm_zone, exact.south)
    assert math.hypot(e - exact.ulx, n + 100_000 - exact.uly) <= S2_DERIVED_ORIGIN_UNCERTAINTY_M


@pytest.mark.parametrize("mgrs, lat, lon", [("31TCJ", 43.7, 1.2), ("55HFA", -35.7, 148.7), ("18SUJ", 39.2, -76.7)])
def test_any_tile_can_be_georeferenced(mgrs: str, lat: float, lon: float) -> None:
    from applications.maritime_threat_detection.geo import pixel_to_latlon

    g = s2_grid(mgrs)
    assert g.origin_source == "derived"
    c = pixel_to_latlon(g, 5490, 5490)  # tile centre
    assert abs(c[0] - lat) < 0.6 and abs(c[1] - lon) < 0.6


def test_southern_hemisphere_round_trip() -> None:
    e, n = latlon_to_utm(-33.9, 151.2, 56, south=True)
    assert 6_000_000 < n < 7_000_000
    assert utm_to_latlon(e, n, 56, south=True) == pytest.approx((-33.9, 151.2), abs=1e-7)


@pytest.mark.parametrize("bad", ["40IDN", "99RDN", "40RJN", "40RDW", "4RDN", "hello"])
def test_bad_mgrs_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        mgrs_square_sw(bad)


def test_tile_metadata_gives_the_exact_origin() -> None:
    xml = ('<n1:Level-2A_Tile_ID xmlns:n1="https://psd-14.sentinel2.eo.esa.int/x"><n1:Geometric_Info><Tile_Geocoding>'
           '<HORIZONTAL_CS_CODE>EPSG:32756</HORIZONTAL_CS_CODE><Geoposition resolution="10"><ULX>600000</ULX>'
           '<ULY>6100000</ULY></Geoposition><Geoposition resolution="20"><ULX>1</ULX><ULY>2</ULY></Geoposition>'
           '</Tile_Geocoding></n1:Geometric_Info></n1:Level-2A_Tile_ID>')
    g = parse_tile_metadata(xml, "T56HLH")
    assert (g.utm_zone, g.south, g.ulx, g.uly, g.origin_source) == (56, True, 600000, 6100000, "metadata")


# ------------------------------------------------------------- column mapping -----


def test_mapping_renames_and_fills_optional_columns() -> None:
    raw = pd.DataFrame({"MMSI": [538012301], "BaseDateTime": ["2026-03-03 06:00"], "LAT": [25.1], "LON": [56.4]})
    out = apply_mapping(raw, {"mmsi": "MMSI", "postime": "BaseDateTime", "lat": "LAT", "lon": "LON"},
                        AIS_REQUIRED, AIS_OPTIONAL, "AIS")
    assert list(out.columns[:4]) == ["mmsi", "postime", "lat", "lon"] and out["sog"].isna().all()


def test_mapping_typos_and_missing_required_fail_loudly() -> None:
    raw = pd.DataFrame({"MMSI": [1], "t": ["x"], "lat": [1.0], "lon": [1.0]})
    with pytest.raises(ValueError, match="unknown canonical"):
        apply_mapping(raw, {"msi": "MMSI"}, AIS_REQUIRED, AIS_OPTIONAL, "AIS")
    with pytest.raises(ValueError, match="not in the file"):
        apply_mapping(raw, {"mmsi": "MMSI_ID"}, AIS_REQUIRED, AIS_OPTIONAL, "AIS")
    with pytest.raises(ValueError, match="required"):
        apply_mapping(raw, {"mmsi": "MMSI"}, AIS_REQUIRED, AIS_OPTIONAL, "AIS")  # postime missing


# --------------------------------------------------------- synthetic dataset -----

T = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)
BOX = {"lat_min": 25.0, "lat_max": 25.2, "lon_min": 56.0, "lon_max": 56.4}
M_PER_DEG = 111_195.0
KN = 0.514444


def _write_dataset(root: Path, clock_offset_h: int, zoned: bool = False, provenance: str = "independent",
                   utc_offset: str = "auto") -> Path:
    """12 ships steaming north at 10 kn, 3 at anchor, one unlit 80 m hull and one 15 m boat.

    AIS timestamps are written on a clock that is UTC + ``clock_offset_h``.
    """
    ais, dets = [], []
    ships = [(538000100 + i, 25.1, 56.02 + 0.03 * i, 10.0) for i in range(12)]
    ships += [(538000200 + i, 25.05, 56.05 + 0.1 * i, 0.0) for i in range(3)]
    for mmsi, lat, lon, sog in ships:
        for k in range(-36, 37):  # every 5 min, +-3 h around the image
            dt = k * 300
            fix_lat = lat + sog * KN * dt / M_PER_DEG
            stamp = T + timedelta(seconds=dt) + timedelta(hours=clock_offset_h)
            ais.append({"MMSI": mmsi, "Time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if zoned else stamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "Latitude": fix_lat, "Longitude": lon, "SOG": sog, "COG": 0.0, "Length": 150})
        dets.append({"scene": "S1", "y": lat, "x": lon, "len": 150, "label": mmsi, "hdg": 0.0})
    dets.append({"scene": "S1", "y": 25.17, "x": 56.38, "len": 80, "label": "", "hdg": 90.0})  # unlit, no AIS
    dets.append({"scene": "S1", "y": 25.15, "x": 56.30, "len": 15, "label": "", "hdg": 90.0})  # small boat
    dets.append({"scene": "S1", "y": 25.15, "x": 56.30, "len": None, "label": "", "hdg": 0.0})  # no length: dropped
    dets.append({"scene": "S9", "y": 25.15, "x": 56.30, "len": 50, "label": "", "hdg": 0.0})  # unknown scene: dropped
    pd.DataFrame(ais).to_csv(root / "ais.csv", index=False)
    pd.DataFrame(dets).to_csv(root / "detections.csv", index=False)
    pd.DataFrame([{"scene_id": "S1", "acquired_at": T.strftime("%Y-%m-%dT%H:%M:%SZ"), **BOX}]).to_csv(
        root / "scenes.csv", index=False)
    manifest = {
        "name": "synthetic_test", "label_provenance": provenance,
        "ais": {"path": "ais.csv", "utc_offset": utc_offset,
                "columns": {"mmsi": "MMSI", "postime": "Time", "lat": "Latitude", "lon": "Longitude",
                            "sog": "SOG", "cog": "COG", "length": "Length"}},
        "imagery": {"format": "georeferenced_csv", "annotations": "detections.csv", "scenes": "scenes.csv",
                    "columns": {"scene_id": "scene", "lat": "y", "lon": "x", "length_m": "len", "heading_deg": "hdg"}},
    }
    path = root / "dataset.json"
    path.write_text(json.dumps(manifest))
    return path


def test_manifest_builds_a_self_contained_store(tmp_path: Path) -> None:
    m = load_manifest(_write_dataset(tmp_path, clock_offset_h=0, utc_offset="+00:00"))
    db = tmp_path / "out.db"
    r = run_manifest(m, db)
    assert r["etl"]["imagery_rows_dropped"] == {"missing_length": 1, "unknown_scene": 1}
    assert r["etl"]["tile_origins"] == {"bbox": 1}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tile = conn.execute("SELECT * FROM optical_tiles").fetchone()
    assert tile["ulx"] is None and inside_chip(tile, 25.1, 56.2) and not inside_chip(tile, 25.1, 56.4)
    run = conn.execute("SELECT dataset_name, label_provenance, imagery_format FROM ingest_runs").fetchone()
    assert tuple(run) == ("synthetic_test", "independent", "georeferenced_csv")
    b = r["cross_reference"]["blind_association"]
    # Independent labels make dark-vessel recall measurable: the unlit 80 m hull is found,
    # the 15 m boat is not flagged, and no labelled AIS ship is called dark.
    assert b["labelled_dark_hulls"] == 1 and b["dark_vessel_recall"] == 1.0 and b["false_dark_rate"] == 0.0
    assert b["precision"] == 1.0


def test_ais_matched_labels_cannot_measure_dark_recall(tmp_path: Path) -> None:
    m = load_manifest(_write_dataset(tmp_path, 0, provenance="ais_matched", utc_offset="+00:00"))
    b = run_manifest(m, tmp_path / "out.db")["cross_reference"]["blind_association"]
    assert isinstance(b["dark_vessel_recall"], str) and "not measurable" in b["dark_vessel_recall"]


def test_auto_sweep_recovers_a_utc_plus_8_clock(tmp_path: Path) -> None:
    m = load_manifest(_write_dataset(tmp_path, clock_offset_h=8))
    r = run_manifest(m, tmp_path / "out.db")
    assert r["ais_utc_offset"] == "+08:00" and r["ais_offset_method"] == "auto", r["offset_sweep"]["reason"]
    assert r["cross_reference"]["blind_association"]["precision"] == 1.0


def test_auto_sweep_refuses_to_guess_without_moving_ships(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, clock_offset_h=8)
    ais = pd.read_csv(tmp_path / "ais.csv")
    ais = ais[ais["SOG"] == 0]  # only anchored ships remain: no evidence about the clock
    ais.to_csv(tmp_path / "ais.csv", index=False)
    r = run_manifest(load_manifest(path), tmp_path / "out.db")
    assert r["ais_offset_method"] == "auto_ambiguous_kept_utc" and r["ais_utc_offset"] == "+00:00"
    assert "ambiguous" in r["offset_sweep"]["reason"]


def test_zoned_timestamps_need_no_offset(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, clock_offset_h=0, zoned=True)
    raw, _, zoned = read_ais(load_manifest(path))
    assert zoned and raw["postime"].dt.tz is None
    r = run_manifest(load_manifest(path), tmp_path / "out.db")
    assert r["ais_offset_method"] == "explicit_in_data" and r["cross_reference"]["blind_association"]["precision"] == 1.0
