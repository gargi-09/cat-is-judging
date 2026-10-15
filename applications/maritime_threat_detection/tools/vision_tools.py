"""Optical-side ground truth: parse Labelme annotations on Sentinel-2 chips.

Each JSON in ``data/raw/json`` annotates one 256 x 256 chip cut from a full Sentinel-2
MGRS tile. The chip's georeference is encoded only in its file name, e.g.::

    S2B_MSIL2A_20260303T064659_N0512_R020_T40RDN_20260303T105730.SAFE_png__x3416_y1315.json
               ^ sensing start (UTC)          ^ MGRS tile                  ^ chip top-left px

Every shape ``label`` is the vessel's MMSI serialised as a float string ("538012301.0").
This module turns those files into typed, georeferenced records. It is the deterministic
"what the satellite actually saw" half of the reconciliation; the agent-facing
``inspect_optical_tile`` tool will be layered on top of it.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from applications.maritime_threat_detection import ANNOTATIONS_DIR, IMAGES_DIR, REPO_ROOT
from applications.maritime_threat_detection.geo import S2_TILE_GRID, S2TileGrid, pixel_to_latlon

_TILE_NAME_RE = re.compile(
    r"^(?P<product>S2[A-D]_MSIL[12][AC]_(?P<sensing>\d{8}T\d{6})_N\d{4}_R\d{3}"
    r"_T(?P<mgrs>\d{2}[A-Z]{3})_\d{8}T\d{6})\.SAFE_png__x(?P<x>\d+)_y(?P<y>\d+)$"
)

# Labels that annotators use for "ship visible, identity unknown". These are the purest
# dark-vessel signal: an optical hull with no transponder identity at all.
_UNKNOWN_LABELS = frozenset({"", "0", "unknown", "none", "null", "nan", "?"})


class UnsupportedShapeError(ValueError):
    """Raised for annotation geometry we cannot measure without guessing."""


class OpticalDetection(BaseModel):
    model_config = ConfigDict(frozen=True)

    shape_index: int
    label_raw: str
    mmsi: int | None = Field(description="Normalised MMSI; None when the label carries no identity")
    shape_type: str
    polygon_px: list[tuple[float, float]] = Field(description="Chip-local pixel coordinates")
    center_px: tuple[float, float]
    center_lat: float
    center_lon: float
    length_m: float = Field(description="Long side of the rotated box at tile GSD")
    width_m: float
    axis_bearing_deg: float = Field(
        description="Long-axis bearing from true north in [0, 180); optics cannot tell bow from stern"
    )


class OpticalTile(BaseModel):
    model_config = ConfigDict(frozen=True)

    tile_id: str
    product_id: str
    mgrs_tile: str
    acquired_at: datetime
    px_x: int
    px_y: int
    width_px: int
    height_px: int
    gsd_m: float
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    json_path: str = Field(description="Repo-relative POSIX path")
    png_path: str | None
    detections: list[OpticalDetection]
    skipped_shapes: int = 0


def normalize_mmsi_label(label: object) -> int | None:
    """Turn an annotation label into an MMSI, or None if it carries no identity.

    ``"538012301.0" -> 538012301``. Non-integral floats and non-positive values are
    rejected rather than truncated: a mangled identity must surface as "unknown", never
    silently collide with a real vessel.
    """
    text = str(label).strip().lower() if label is not None else ""
    if text in _UNKNOWN_LABELS:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or value != int(value):
        return None
    return int(value)


def parse_tile_name(stem: str) -> tuple[str, str, datetime, int, int]:
    """Return ``(product_id, mgrs_tile, sensing_start_utc, chip_px_x, chip_px_y)``."""
    m = _TILE_NAME_RE.match(stem)
    if m is None:
        raise ValueError(f"Unrecognised Sentinel-2 chip name: {stem!r}")
    sensing = datetime.strptime(m["sensing"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return m["product"], m["mgrs"], sensing, int(m["x"]), int(m["y"])


def make_tile_id(mgrs: str, sensing: datetime, px_x: int, px_y: int) -> str:
    """Short, CLI-friendly id that is still unique across products and dates."""
    return f"T{mgrs}_{sensing:%Y%m%d}_x{px_x}_y{px_y}"


def _box_geometry(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Return (long_side_px, short_side_px, long-axis bearing from north in degrees)."""
    if len(points) != 4:
        raise UnsupportedShapeError(f"expected a 4-corner rotated box, got {len(points)} points")
    (x0, y0), (x1, y1), (x2, y2) = points[0], points[1], points[2]
    e1 = (x1 - x0, y1 - y0)
    e2 = (x2 - x1, y2 - y1)
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    (dx, dy), long_px, short_px = (e1, l1, l2) if l1 >= l2 else (e2, l2, l1)
    # Image rows grow southward, so north is -dy. UTM grid convergence here is <0.5 deg
    # (tiles sit ~0.4 deg from the zone 40 central meridian), so grid north ~ true north.
    bearing = math.degrees(math.atan2(dx, -dy)) % 180.0
    return long_px, short_px, bearing


def load_tile(json_path: Path, *, grid_registry: dict[str, S2TileGrid] = S2_TILE_GRID) -> OpticalTile:
    product_id, mgrs, sensing, px_x, px_y = parse_tile_name(json_path.stem)
    grid = grid_registry.get(mgrs)
    if grid is None:
        raise KeyError(f"No Sentinel-2 grid origin registered for MGRS tile {mgrs}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    width, height = int(payload["imageWidth"]), int(payload["imageHeight"])

    detections: list[OpticalDetection] = []
    skipped = 0
    for idx, shape in enumerate(payload.get("shapes", [])):
        points = [(float(x), float(y)) for x, y in shape["points"]]
        try:
            long_px, short_px, bearing = _box_geometry(points)
        except UnsupportedShapeError:
            # Counted, not guessed: a fabricated length would poison dimension checks later.
            skipped += 1
            continue
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        lat, lon = pixel_to_latlon(grid, px_x + cx, px_y + cy)
        detections.append(
            OpticalDetection(
                shape_index=idx,
                label_raw=str(shape.get("label", "")),
                mmsi=normalize_mmsi_label(shape.get("label")),
                shape_type=str(shape.get("shape_type", "")),
                polygon_px=points,
                center_px=(cx, cy),
                center_lat=lat,
                center_lon=lon,
                length_m=long_px * grid.gsd_m,
                width_m=short_px * grid.gsd_m,
                axis_bearing_deg=bearing,
            )
        )

    corners = [pixel_to_latlon(grid, px_x + dx, px_y + dy) for dx in (0, width) for dy in (0, height)]
    png = IMAGES_DIR / f"{json_path.stem}.png"
    return OpticalTile(
        tile_id=make_tile_id(mgrs, sensing, px_x, px_y),
        product_id=product_id,
        mgrs_tile=mgrs,
        acquired_at=sensing,
        px_x=px_x,
        px_y=px_y,
        width_px=width,
        height_px=height,
        gsd_m=grid.gsd_m,
        lat_min=min(c[0] for c in corners),
        lat_max=max(c[0] for c in corners),
        lon_min=min(c[1] for c in corners),
        lon_max=max(c[1] for c in corners),
        json_path=_repo_relative(json_path),
        png_path=_repo_relative(png) if png.exists() else None,
        detections=detections,
        skipped_shapes=skipped,
    )


def load_all_tiles(annotations_dir: Path = ANNOTATIONS_DIR) -> list[OpticalTile]:
    return [load_tile(p) for p in sorted(annotations_dir.glob("*.json"))]


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()
