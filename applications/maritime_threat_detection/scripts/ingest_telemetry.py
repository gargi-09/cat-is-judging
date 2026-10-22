"""ETL: raw AIS spreadsheet + Sentinel-2 annotations -> indexed SQLite ground truth.

Usage (from the repository root)::

    python -m applications.maritime_threat_detection.scripts.ingest_telemetry
    python -m applications.maritime_threat_detection.scripts.ingest_telemetry --ais-utc-offset +08:00
    python -m applications.maritime_threat_detection.scripts.ingest_telemetry --manifest path/to/dataset.json

The database is rebuilt from scratch on every run and swapped in atomically. Ground truth
for an auditor must be reproducible from raw inputs; incremental upserts would let stale
or hand-edited rows survive and silently change what "true" means.

Timezone: the source ``postime`` column is naive. We read it as UTC, chosen empirically:
sweeping offsets from -1 h to +10 h, UTC minimises the share of MMSI-labelled hulls whose
AIS track is >500 m from the optical position at acquisition (2.5%, vs 3.8% at -1 h,
7.8% at +1 h and 8.6% at UTC+8).
The ``eta`` column's explicit ``+08`` is a different field convention and is a trap — a
wrong offset turns ordinary ships into dozens of false "spoofing" alerts. The offset used
is recorded in ``ingest_runs``. For a new dataset, ``"utc_offset": "auto"`` in its manifest
runs a label-free sweep instead (``clock_offset.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from applications.maritime_threat_detection import DATA_PROCESSED, DATA_RAW, DB_PATH
from applications.maritime_threat_detection.clock_offset import SweepResult, sweep
from applications.maritime_threat_detection.dataset import (
    DatasetManifest,
    bundled_manifest,
    load_imagery,
    load_manifest,
    read_ais,
)
from applications.maritime_threat_detection.db import apply_schema, connect
from applications.maritime_threat_detection.reconciliation import cross_reference
from applications.maritime_threat_detection.tools.vision_tools import OpticalTile

REQUIRED_COLUMNS: tuple[str, ...] = (
    "mmsi", "postime", "lon", "lat", "rot", "sog", "cog", "hdg", "status", "imo", "callsign",
    "vessel_name", "vesseltype", "a", "b", "c", "d", "length", "width", "draught", "dest",
    "eta", "flag_ctry",
)
STATIC_COLUMNS: tuple[str, ...] = (
    "imo", "callsign", "vessel_name", "vessel_type", "dim_a", "dim_b", "dim_c", "dim_d",
    "length_m", "width_m", "flag_ctry",
)


# --------------------------------------------------------------------------- AIS -----


def find_ais_source(raw_dir: Path = DATA_RAW) -> Path:
    """Locate the AIS workbook; tolerates the doubled ``.xlsx.xlsx`` extension seen in the drop."""
    matches = sorted(raw_dir.glob("raw_ais_data*.xlsx"))
    if not matches:
        raise FileNotFoundError(f"No raw_ais_data*.xlsx under {raw_dir}")
    return matches[0]


def parse_utc_offset(text: str) -> timedelta:
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text.strip())
    if m is None:
        raise argparse.ArgumentTypeError(f"UTC offset must look like +08:00, got {text!r}")
    sign = 1 if m[1] == "+" else -1
    return sign * timedelta(hours=int(m[2]), minutes=int(m[3]))


def clean_ais(raw: pd.DataFrame, utc_offset: timedelta) -> tuple[pd.DataFrame, pd.DataFrame, Counter[str], int]:
    """Validate and normalise raw AIS rows.

    Returns ``(positions, vessels, dropped_reason_counts, n_static_conflicts)``. Invalid
    rows are dropped with a reason rather than repaired: an imputed position is exactly the
    kind of unfounded "fact" the auditor exists to catch.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"AIS source is missing columns: {missing}")

    df = raw.loc[:, list(REQUIRED_COLUMNS)].copy()
    dropped: Counter[str] = Counter()

    def drop(mask: pd.Series, reason: str) -> None:
        nonlocal df
        n = int(mask.sum())
        if n:
            dropped[reason] += n
            df = df.loc[~mask]

    # Identity: MMSIs are 9-digit integers. Excel may hand us floats or strings.
    df["mmsi"] = pd.to_numeric(df["mmsi"], errors="coerce")
    drop(df["mmsi"].isna() | (df["mmsi"] % 1 != 0) | ~df["mmsi"].between(100_000_000, 999_999_999), "invalid_mmsi")
    df["mmsi"] = df["mmsi"].astype("int64")

    # Time: naive provider-local -> UTC.
    df["postime"] = pd.to_datetime(df["postime"], errors="coerce")
    drop(df["postime"].isna(), "invalid_postime")
    utc = df["postime"] - utc_offset
    df["postime_epoch"] = (utc.astype("datetime64[s]").astype("int64")).astype("int64")
    df["postime"] = utc.dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Position: drop impossible coordinates and the (0, 0) "null island" default.
    for col in ("lat", "lon", "sog", "cog", "hdg", "rot", "draught", "width"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    drop(
        df["lat"].isna() | df["lon"].isna() | ~df["lat"].between(-90, 90) | ~df["lon"].between(-180, 180)
        | ((df["lat"] == 0) & (df["lon"] == 0)),
        "invalid_position",
    )

    # ITU-R M.1371 "not available" sentinels -> NULL, so they never read as real values.
    df.loc[(df["sog"] >= 102.3) | (df["sog"] < 0), "sog"] = pd.NA
    df.loc[(df["cog"] >= 360) | (df["cog"] < 0), "cog"] = pd.NA
    df.loc[(df["hdg"] >= 360) | (df["hdg"] < 0), "hdg"] = pd.NA  # covers 511
    df.loc[df["rot"] == -128, "rot"] = pd.NA
    df["nav_status"] = pd.to_numeric(df["status"], errors="coerce").where(lambda s: s.between(0, 15))

    # Static identity fields.
    df["imo"] = pd.to_numeric(df["imo"], errors="coerce").where(lambda s: s > 0)
    df["vessel_type"] = pd.to_numeric(df["vesseltype"], errors="coerce").where(lambda s: s > 0)
    for src, dst in (("a", "dim_a"), ("b", "dim_b"), ("c", "dim_c"), ("d", "dim_d")):
        df[dst] = pd.to_numeric(df[src], errors="coerce")
    # Overall length/beam are by definition a+b / c+d; fill gaps from the antenna offsets
    # (this is derivation from the same message, not imputation).
    df["length_m"] = pd.to_numeric(df["length"], errors="coerce")
    df["length_m"] = df["length_m"].where(df["length_m"] > 0, df["dim_a"] + df["dim_b"])
    df["width_m"] = df["width"].where(df["width"] > 0, df["dim_c"] + df["dim_d"])
    df.loc[df["width_m"] <= 0, "width_m"] = pd.NA

    for col in ("callsign", "vessel_name", "dest", "flag_ctry"):
        s = df[col].astype("string").str.strip().str.upper()
        df[col] = s.where(s.notna() & (s != "") & (s != "NAN"))

    eta = pd.to_datetime(df["eta"].astype("string"), errors="coerce", utc=True, format="ISO8601")
    df["eta"] = eta.dt.strftime("%Y-%m-%dT%H:%M:%SZ").where(eta.notna())

    dupes = df.duplicated(["mmsi", "postime_epoch"], keep="last")
    drop(dupes, "duplicate_mmsi_postime")

    # Static registry: one row per MMSI, most frequent value per field. Count MMSIs whose
    # static fields disagree across messages — itself a spoofing / identity-sharing signal.
    def mode(s: pd.Series) -> Any:
        s = s.dropna()
        return s.mode().iloc[0] if not s.empty else None

    grouped = df.groupby("mmsi", sort=True)
    vessels = grouped[list(STATIC_COLUMNS)].agg(mode)
    conflicts = int((grouped[list(STATIC_COLUMNS)].nunique() > 1).any(axis=1).sum())
    vessels["n_positions"] = grouped.size()
    vessels = vessels.reset_index()

    positions = df[
        ["mmsi", "postime", "postime_epoch", "lat", "lon", "sog", "cog", "hdg", "rot",
         "nav_status", "draught", "dest", "eta"]
    ].sort_values(["mmsi", "postime_epoch"])
    return positions, vessels, dropped, conflicts


def _records(df: pd.DataFrame) -> list[tuple[Any, ...]]:
    """DataFrame -> tuples with NaN/NA mapped to None and numpy scalars to Python."""
    out = df.astype(object).where(df.notna(), None)
    return [tuple(v.item() if hasattr(v, "item") else v for v in row) for row in out.itertuples(index=False)]


# ------------------------------------------------------------------------- load ------


def write_ais(conn: Any, positions: pd.DataFrame, vessels: pd.DataFrame) -> None:
    vcols = ["mmsi", *STATIC_COLUMNS, "n_positions"]
    conn.executemany(
        f"INSERT INTO vessels ({', '.join(vcols)}) VALUES ({', '.join('?' * len(vcols))})",
        _records(vessels[vcols]),
    )
    pcols = list(positions.columns)
    conn.executemany(
        f"INSERT INTO ais_positions ({', '.join(pcols)}) VALUES ({', '.join('?' * len(pcols))})",
        _records(positions),
    )


def write_optical(conn: Any, tiles: list[OpticalTile]) -> int:
    n = 0
    for t in tiles:
        conn.execute(
            "INSERT INTO optical_tiles (tile_id, product_id, mgrs_tile, acquired_at, acquired_epoch, px_x, px_y, "
            "width_px, height_px, gsd_m, utm_zone, utm_south, ulx, uly, origin_source, lat_min, lat_max, lon_min, "
            "lon_max, n_detections, json_path, png_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.tile_id, t.product_id, t.mgrs_tile, t.acquired_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                int(t.acquired_at.timestamp()), t.px_x, t.px_y, t.width_px, t.height_px, t.gsd_m,
                t.utm_zone, None if t.utm_south is None else int(t.utm_south), t.ulx, t.uly, t.origin_source,
                t.lat_min, t.lat_max, t.lon_min, t.lon_max, len(t.detections), t.json_path, t.png_path,
            ),
        )
        for d in t.detections:
            conn.execute(
                "INSERT INTO optical_detections (tile_id, shape_index, label_raw, mmsi, center_lat, center_lon, "
                "center_px_x, center_px_y, length_m, width_m, axis_bearing_deg, shape_type, polygon_px) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.tile_id, d.shape_index, d.label_raw, d.mmsi, d.center_lat, d.center_lon,
                    *(d.center_px or (None, None)), d.length_m, d.width_m, d.axis_bearing_deg,
                    d.shape_type, json.dumps(d.polygon_px) if d.polygon_px else None,
                ),
            )
            n += 1
    return n


# ----------------------------------------------------------------------- report ------


def table_summary(conn: Any) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        idx = [r[1] for r in conn.execute(f"PRAGMA index_list({name})") if not r[1].startswith("sqlite_autoindex")]
        tables[name] = {"rows": conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0], "columns": len(cols), "indexes": idx}
    out: dict[str, Any] = {"tables": tables}
    a = conn.execute(
        "SELECT MIN(postime), MAX(postime), MIN(lat), MAX(lat), MIN(lon), MAX(lon), "
        "SUM(sog IS NULL), SUM(cog IS NULL), SUM(hdg IS NULL), SUM(rot IS NULL) FROM ais_positions"
    ).fetchone()
    per_vessel = [r[0] for r in conn.execute("SELECT n_positions FROM vessels")]
    out["ais_profile"] = {
        "time_range_utc": [a[0], a[1]],
        "bbox": {"lat": [round(a[2], 4), round(a[3], 4)], "lon": [round(a[4], 4), round(a[5], 4)]},
        "null_after_cleaning": {"sog": a[6], "cog": a[7], "hdg": a[8], "rot": a[9]},
        "fixes_per_vessel": {"min": min(per_vessel), "median": statistics.median(per_vessel), "max": max(per_vessel)},
    }
    out["optical_profile"] = {
        "tiles_by_mgrs": dict(conn.execute("SELECT mgrs_tile, COUNT(*) FROM optical_tiles GROUP BY 1").fetchall()),
        "acquired_at": [r[0] for r in conn.execute("SELECT DISTINCT acquired_at FROM optical_tiles")],
        "detections_per_tile": dict(
            conn.execute("SELECT n_detections, COUNT(*) FROM optical_tiles GROUP BY 1 ORDER BY 1").fetchall()
        ),
        "unique_labels": conn.execute("SELECT COUNT(DISTINCT label_raw) FROM optical_detections").fetchone()[0],
        "tiles_missing_png": conn.execute("SELECT COUNT(*) FROM optical_tiles WHERE png_path IS NULL").fetchone()[0],
        "optical_length_m": dict(zip(
            ("min", "avg", "max"),
            (round(v, 1) for v in conn.execute(
                "SELECT MIN(length_m), AVG(length_m), MAX(length_m) FROM optical_detections").fetchone()),
        )),
    }
    return out


# ------------------------------------------------------------------------- main ------


def _memory_store(positions: pd.DataFrame, vessels: pd.DataFrame, tiles: list[OpticalTile]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    write_ais(conn, positions, vessels)
    write_optical(conn, tiles)
    return conn


def run_manifest(m: DatasetManifest, db_path: Path | None = None, utc_offset: str | None = None) -> dict[str, Any]:
    """Build one dataset's ground-truth store from its manifest; returns the ingest report."""
    started = datetime.now(timezone.utc)
    db_path = db_path or m.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw, source, zoned = read_ais(m)
    tiles, imagery_dropped = load_imagery(m)

    requested = utc_offset or m.ais.utc_offset
    sweep_result: SweepResult | None = None
    if zoned:
        offset_text, method = "+00:00", "explicit_in_data"  # timestamps carried their own zone
    elif requested == "auto":
        positions0, vessels0, _, _ = clean_ais(raw, timedelta(0))
        with closing(_memory_store(positions0, vessels0, tiles)) as mem:
            sweep_result = sweep(mem)
        offset_text = sweep_result.offset_text or "+00:00"
        method = "auto" if sweep_result.offset_h is not None else "auto_ambiguous_kept_utc"
    else:
        parse_utc_offset(requested)
        offset_text, method = requested, "manual"

    positions, vessels, dropped, conflicts = clean_ais(raw, parse_utc_offset(offset_text))
    tmp = db_path.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    with connect(tmp) as conn:
        apply_schema(conn)
        write_ais(conn, positions, vessels)
        n_det = write_optical(conn, tiles)
        conn.execute(
            "INSERT INTO ingest_runs (started_at, source_file, source_sha256, ais_utc_offset, rows_read, rows_loaded, "
            "rows_dropped_json, tiles_loaded, detections_loaded, dataset_name, imagery_format, label_provenance, "
            "ais_offset_method, offset_sweep_json, manifest_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started.strftime("%Y-%m-%dT%H:%M:%SZ"), source.name,
                hashlib.sha256(source.read_bytes()).hexdigest(), offset_text,
                len(raw), len(positions), json.dumps(dict(dropped)), len(tiles), n_det,
                m.name, m.imagery.format, m.label_provenance, method,
                json.dumps(sweep_result.table) if sweep_result else None, m.model_dump_json(),
            ),
        )
        conn.commit()
        conn.execute("ANALYZE")
        summary = table_summary(conn)
        xref = cross_reference(conn, m.label_provenance)
    conn.close()
    os.replace(tmp, db_path)  # atomic swap: readers never see a half-built database

    report = {
        "dataset": m.name,
        "database": db_path.as_posix(),
        "source": source.name,
        "ais_utc_offset": offset_text,
        "ais_offset_method": method,
        "offset_sweep": ({"chosen": sweep_result.offset_text, "reason": sweep_result.reason, "table": sweep_result.table}
                         if sweep_result else None),
        "label_provenance": m.label_provenance,
        "etl": {
            "rows_read": len(raw),
            "rows_loaded": len(positions),
            "rows_dropped": dict(dropped),
            "vessels_with_conflicting_static_fields": conflicts,
            "imagery_rows_dropped": dict(imagery_dropped),
            "tile_origins": dict(Counter(t.origin_source for t in tiles)),
        },
        **summary,
        "cross_reference": xref,
    }
    name = "ingest_report.json" if db_path.name == DB_PATH.name else f"{db_path.stem}_ingest_report.json"
    (db_path.parent / name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run(source: Path, db_path: Path, utc_offset_text: str) -> dict[str, Any]:
    """The bundled dataset (the original entry point)."""
    return run_manifest(bundled_manifest(source, utc_offset_text), db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=None, help="dataset.json describing another dataset")
    parser.add_argument("--source", type=Path, default=None, help="AIS workbook (default: auto-detect in data/raw)")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--ais-utc-offset", default=None,
                        help="Offset of naive timestamps, e.g. +08:00, or 'auto' for the label-free sweep")
    args = parser.parse_args(argv)

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    if args.manifest:
        report = run_manifest(load_manifest(args.manifest), args.db, args.ais_utc_offset)
    else:
        report = run(args.source or find_ais_source(), args.db or DB_PATH, args.ais_utc_offset or "+00:00")
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
