"""Dataset adapter: describe a new AIS + imagery dataset in a manifest, not in code.

A manifest (``dataset.json``) says where the files are, which source column means what,
what timezone the AIS clock is in (or ``"auto"``), and how the labels were made. The ETL
turns it into its own self-contained, read-only ground-truth store.

Two imagery formats are supported:

* ``sentinel2_labelme`` — the format of the bundled data: Labelme JSON per Sentinel-2
  chip, georeferenced from the chip name. Tile origins come from each product's
  ``MTD_TL.xml`` when given (exact), else the registry, else the MGRS square (~45 m).
* ``georeferenced_csv`` — detections already in lat/lon (e.g. SAR or other optical
  detection datasets): one CSV of detections and one CSV of scene footprints.

Label provenance is part of the manifest because it decides what can be measured: labels
made by matching hulls to AIS can't measure dark-vessel recall (that would be circular).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from applications.maritime_threat_detection import ANNOTATIONS_DIR, DATA_PROCESSED, DATA_RAW, IMAGES_DIR
from applications.maritime_threat_detection.geo import S2TileGrid, parse_tile_metadata
from applications.maritime_threat_detection.tools.vision_tools import (
    OpticalDetection,
    OpticalTile,
    load_all_tiles,
    normalize_mmsi_label,
)

# The ETL's canonical AIS columns. Only these four are required; the rest are filled with
# "not available" when the source doesn't have them.
AIS_REQUIRED: tuple[str, ...] = ("mmsi", "postime", "lat", "lon")
AIS_OPTIONAL: tuple[str, ...] = (
    "rot", "sog", "cog", "hdg", "status", "imo", "callsign", "vessel_name", "vesseltype", "a", "b", "c", "d",
    "length", "width", "draught", "dest", "eta", "flag_ctry",
)
DETECTION_REQUIRED: tuple[str, ...] = ("scene_id", "lat", "lon", "length_m")
DETECTION_OPTIONAL: tuple[str, ...] = ("label", "width_m", "heading_deg")
SCENE_REQUIRED: tuple[str, ...] = ("scene_id", "acquired_at", "lat_min", "lat_max", "lon_min", "lon_max")
SCENE_OPTIONAL: tuple[str, ...] = ("image",)

LabelProvenance = Literal["ais_matched", "independent", "none"]


class AISSource(BaseModel):
    path: str  # relative to the manifest's folder
    sheet: int | str = 0  # spreadsheet tab, for .xlsx
    columns: dict[str, str] = {}  # canonical name -> source column; unmapped names are looked up as-is
    utc_offset: str = "+00:00"  # offset of naive timestamps, or "auto" for the label-free sweep


class ImagerySource(BaseModel):
    format: Literal["sentinel2_labelme", "georeferenced_csv"]
    annotations: str  # Labelme folder, or the detections CSV
    images: str | None = None  # folder of chip PNGs (Labelme format)
    scenes: str | None = None  # scene footprints CSV (georeferenced format)
    tile_metadata: dict[str, str] = {}  # MGRS tile -> MTD_TL.xml path, for exact origins
    columns: dict[str, str] = {}  # canonical detection column -> source column
    scene_columns: dict[str, str] = {}  # canonical scene column -> source column


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    ais: AISSource
    imagery: ImagerySource
    label_provenance: LabelProvenance
    base_dir: Path = Field(default=Path("."), exclude=True)

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", v):
            raise ValueError("name must be a lowercase slug (letters, digits, _ and -), max 64 chars")
        return v

    def resolve(self, rel: str) -> Path:
        """Paths are relative to the manifest; ``{data_raw}`` means the configured raw-data folder."""
        p = Path(rel.replace("{data_raw}", str(DATA_RAW)))
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        return DATA_PROCESSED / f"{self.name}.db"


def load_manifest(path: Path) -> DatasetManifest:
    m = DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return m.model_copy(update={"base_dir": path.resolve().parent})


def bundled_manifest(ais_path: Path, utc_offset: str = "+00:00") -> DatasetManifest:
    """The Gulf of Oman drop the project was built on (labels were made by matching to AIS)."""
    return DatasetManifest(
        name="gulf_of_oman_2026_03_03",
        description="One day of AIS and 183 Sentinel-2 chips, Gulf of Oman, 2026-03-03",
        ais=AISSource(path=ais_path.name, utc_offset=utc_offset),
        imagery=ImagerySource(format="sentinel2_labelme", annotations=ANNOTATIONS_DIR.name, images=IMAGES_DIR.name),
        label_provenance="ais_matched",
        base_dir=DATA_RAW,
    )


# ---------------------------------------------------------------------- AIS -----


def _read_table(path: Path, sheet: int | str = 0) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
    if suffix in (".csv", ".txt"):
        return pd.read_csv(path)
    raise ValueError(f"Unsupported table format {path.suffix!r} (use .xlsx or .csv)")


def apply_mapping(raw: pd.DataFrame, mapping: dict[str, str], required: tuple[str, ...],
                  optional: tuple[str, ...], what: str) -> pd.DataFrame:
    """Rename source columns to canonical names; missing optional columns become all-NA.

    Unknown canonical names in the mapping, mapped columns absent from the source, and
    missing required columns are errors, so a typo can't silently drop a field.
    """
    known = set(required) | set(optional)
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise ValueError(f"{what}: unknown canonical column(s) {unknown}; expected some of {sorted(known)}")
    absent = sorted(src for src in mapping.values() if src not in raw.columns)
    if absent:
        raise ValueError(f"{what}: mapped source column(s) not in the file: {absent}")
    out = pd.DataFrame(index=raw.index)
    for col in (*required, *optional):
        src = mapping.get(col, col)
        out[col] = raw[src] if src in raw.columns else pd.NA
    missing = [c for c in required if out[c].isna().all()]
    if missing:
        raise ValueError(f"{what}: required column(s) {missing} are missing or empty; map them in the manifest")
    return out


def read_ais(m: DatasetManifest) -> tuple[pd.DataFrame, Path, bool]:
    """Raw AIS in the ETL's canonical columns, plus its source path and whether timestamps
    carried their own timezone (then they are converted to UTC here and no offset applies)."""
    path = m.resolve(m.ais.path)
    raw = apply_mapping(_read_table(path, m.ais.sheet), m.ais.columns, AIS_REQUIRED, AIS_OPTIONAL, "AIS")
    zoned = raw["postime"].astype("string").str.contains(r"(?:Z|[+-]\d{2}:?\d{2})\s*$", regex=True, na=False)
    if zoned.any():
        if not zoned.all():
            raise ValueError("AIS timestamps mix zoned and naive values; normalise them first")
        raw["postime"] = pd.to_datetime(raw["postime"], utc=True, errors="coerce").dt.tz_localize(None)
    return raw, path, bool(zoned.any())


# ------------------------------------------------------------------ imagery -----


def tile_origins(m: DatasetManifest) -> dict[str, S2TileGrid]:
    return {k.upper().removeprefix("T"): parse_tile_metadata(m.resolve(v).read_text(encoding="utf-8"), k)
            for k, v in m.imagery.tile_metadata.items()}


def _utc(text: str) -> datetime:
    ts = pd.Timestamp(text)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime().replace(tzinfo=timezone.utc)


def load_georeferenced(m: DatasetManifest) -> tuple[list[OpticalTile], Counter[str]]:
    """Scenes + detections already in lat/lon. Rows that can't be used are dropped with a reason."""
    img = m.imagery
    if not img.scenes:
        raise ValueError("georeferenced_csv needs a 'scenes' CSV with each scene's footprint and time")
    scenes = apply_mapping(_read_table(m.resolve(img.scenes)), img.scene_columns, SCENE_REQUIRED, SCENE_OPTIONAL,
                           "scenes")
    dets = apply_mapping(_read_table(m.resolve(img.annotations)), img.columns, DETECTION_REQUIRED,
                         DETECTION_OPTIONAL, "detections")
    dropped: Counter[str] = Counter()
    for c in ("lat", "lon", "length_m", "width_m", "heading_deg"):
        dets[c] = pd.to_numeric(dets[c], errors="coerce")

    by_scene: dict[str, list[OpticalDetection]] = {}
    known = {str(s) for s in scenes["scene_id"]}
    boxes = {str(r.scene_id): r for r in scenes.itertuples(index=False)}
    for i, r in enumerate(dets.itertuples(index=False)):
        sid = str(r.scene_id)
        if sid not in known:
            dropped["unknown_scene"] += 1
            continue
        if pd.isna(r.lat) or pd.isna(r.lon):
            dropped["invalid_position"] += 1
            continue
        if pd.isna(r.length_m) or r.length_m <= 0:
            dropped["missing_length"] += 1  # the 30 m dark-vessel gate needs it; never guessed
            continue
        b = boxes[sid]
        if not (b.lat_min <= r.lat <= b.lat_max and b.lon_min <= r.lon <= b.lon_max):
            dropped["outside_scene_footprint"] += 1
            continue
        label = "" if pd.isna(r.label) else str(r.label)
        by_scene.setdefault(sid, []).append(OpticalDetection(
            shape_index=i, label_raw=label, mmsi=normalize_mmsi_label(label), center_lat=float(r.lat),
            center_lon=float(r.lon), length_m=float(r.length_m),
            width_m=None if pd.isna(r.width_m) else float(r.width_m),
            axis_bearing_deg=None if pd.isna(r.heading_deg) else float(r.heading_deg) % 180.0,
            shape_type="point"))

    tiles = []
    for r in scenes.itertuples(index=False):
        sid = str(r.scene_id)
        image = None if pd.isna(r.image) else str(m.resolve(str(r.image)))
        tiles.append(OpticalTile(
            tile_id=sid, acquired_at=_utc(str(r.acquired_at)), origin_source="bbox",
            lat_min=float(r.lat_min), lat_max=float(r.lat_max), lon_min=float(r.lon_min), lon_max=float(r.lon_max),
            json_path=str(m.resolve(img.annotations)), png_path=image, detections=by_scene.get(sid, [])))
    return tiles, dropped


def load_imagery(m: DatasetManifest) -> tuple[list[OpticalTile], Counter[str]]:
    if m.imagery.format == "georeferenced_csv":
        return load_georeferenced(m)
    tiles = load_all_tiles(m.resolve(m.imagery.annotations), origins=tile_origins(m),
                           images_dir=m.resolve(m.imagery.images or "png"))
    return tiles, Counter({"unsupported_shape": sum(t.skipped_shapes for t in tiles)} if any(
        t.skipped_shapes for t in tiles) else {})


def describe(m: DatasetManifest) -> dict[str, object]:
    """A dry-run check of a manifest: does every file exist, and what will be loaded."""
    paths = {"ais": m.resolve(m.ais.path), "annotations": m.resolve(m.imagery.annotations)}
    if m.imagery.scenes:
        paths["scenes"] = m.resolve(m.imagery.scenes)
    if m.imagery.images:
        paths["images"] = m.resolve(m.imagery.images)
    paths |= {f"tile_metadata:{k}": m.resolve(v) for k, v in m.imagery.tile_metadata.items()}
    return {"name": m.name, "db_path": m.db_path.as_posix(), "label_provenance": m.label_provenance,
            "imagery_format": m.imagery.format, "utc_offset": m.ais.utc_offset,
            "files": {k: {"path": p.as_posix(), "exists": p.exists()} for k, p in paths.items()},
            "manifest": json.loads(m.model_dump_json())}
