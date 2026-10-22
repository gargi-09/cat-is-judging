"""Deterministic geodesy for reconciling Sentinel-2 pixels with AIS lat/lon.

The annotation JSONs carry no coordinates — only a Sentinel-2 product name, an MGRS tile
id and the crop's pixel offset (``..._T40RDN_...__x3416_y1315``). Georeferencing therefore
has to be reconstructed from the Sentinel-2 tiling grid: each MGRS tile is a 10 980 x 10 980
pixel raster at 10 m GSD in its UTM zone, anchored at a fixed upper-left corner.

We implement WGS84 <-> UTM (Snyder's series, sub-millimetre at these latitudes) in pure
Python instead of pulling in ``pyproj``: the auditor's ground truth must be reproducible
and dependency-light, and a handful of tiles per dataset don't justify a GDAL/PROJ stack.

Tile origins come from, in order of preference: the product's own tile metadata
(``MTD_TL.xml``, exact), the registry below (exact, validated), or the MGRS 100 km square
the tile is named after (derived; within ~45 m of the exact origin, see ``s2_grid``).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

_A = 6_378_137.0  # WGS84 semi-major axis (m)
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_FALSE_EASTING = 500_000.0
_FALSE_NORTHING_SOUTH = 10_000_000.0

EARTH_RADIUS_M = 6_371_008.8


OriginSource = Literal["metadata", "registered", "derived"]


@dataclass(frozen=True)
class S2TileGrid:
    """Upper-left anchor of one Sentinel-2 MGRS tile in its UTM zone."""

    mgrs: str
    utm_zone: int
    ulx: float
    uly: float
    gsd_m: float = 10.0
    south: bool = False
    origin_source: OriginSource = "registered"


# Origins follow the Sentinel-2 tiling grid (100 km MGRS squares, 109.8 km tiles, 9 780 m
# overlap). Validated against ~120 stationary vessels at acquisition time: implied origins
# agree to within 3-10 px per tile. That residual is physical, not a grid error — AIS reports
# the GPS antenna (~50 m aft of hull centre here), and ships at one anchorage swing to the
# same current, so the offset is systematic per anchorage. We do not fit it away.
S2_TILE_GRID: dict[str, S2TileGrid] = {
    "40RDN": S2TileGrid("40RDN", 40, 399_960.0, 2_800_020.0),
    "40RDP": S2TileGrid("40RDP", 40, 399_960.0, 2_900_020.0),
}


def _central_meridian(zone: int) -> float:
    return math.radians(zone * 6 - 183)


def latlon_to_utm(lat: float, lon: float, zone: int, south: bool = False) -> tuple[float, float]:
    """WGS84 geographic -> UTM (easting, northing) in metres; ``south`` adds the 10 000 km false northing."""
    phi = math.radians(lat)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (math.radians(lon) - _central_meridian(zone))
    m = _A * (
        (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256) * phi
        - (3 * _E2 / 8 + 3 * _E2**2 / 32 + 45 * _E2**3 / 1024) * math.sin(2 * phi)
        + (15 * _E2**2 / 256 + 45 * _E2**3 / 1024) * math.sin(4 * phi)
        - (35 * _E2**3 / 3072) * math.sin(6 * phi)
    )
    easting = _K0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * _EP2) * a**5 / 120
    ) + _FALSE_EASTING
    northing = _K0 * (
        m
        + n
        * math.tan(phi)
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * _EP2) * a**6 / 720
        )
    )
    return easting, northing + (_FALSE_NORTHING_SOUTH if south else 0.0)


def utm_to_latlon(easting: float, northing: float, zone: int, south: bool = False) -> tuple[float, float]:
    """UTM (easting, northing) -> WGS84 (lat, lon) in degrees."""
    x = easting - _FALSE_EASTING
    northing -= _FALSE_NORTHING_SOUTH if south else 0.0
    mu = (northing / _K0) / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - _E2 * sin1**2)
    t1 = tan1**2
    c1 = _EP2 * cos1**2
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1**2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lon = _central_meridian(zone) + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
    ) / cos1
    return math.degrees(lat), math.degrees(lon)


def pixel_to_latlon(grid: S2TileGrid, px_x: float, px_y: float) -> tuple[float, float]:
    """Full-tile pixel coordinates (column, row; may be fractional) -> (lat, lon)."""
    return utm_to_latlon(grid.ulx + px_x * grid.gsd_m, grid.uly - px_y * grid.gsd_m, grid.utm_zone, grid.south)


def latlon_to_pixel(grid: S2TileGrid, lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) -> full-tile pixel coordinates (column, row)."""
    easting, northing = latlon_to_utm(lat, lon, grid.utm_zone, grid.south)
    return (easting - grid.ulx) / grid.gsd_m, (grid.uly - northing) / grid.gsd_m


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres — used for AIS-vs-optical position residuals."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# ----------------------------------------------------------- MGRS -> tile origin -----

_BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8 deg latitude bands from 80 S (X is 12 deg)
_COL_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROWS = "ABCDEFGHJKLMNPQRSTUV"
# A derived origin is the MGRS square's corner; published Sentinel-2 origins sit a few tens
# of metres from it (registered tiles: 40 m in x, 20 m in y). Well inside the 500 m
# corroboration radius, but stated so nobody mistakes a derived origin for an exact one.
S2_DERIVED_ORIGIN_UNCERTAINTY_M: float = 45.0


def mgrs_square_sw(mgrs: str) -> tuple[int, bool, float, float]:
    """``"40RDN"`` -> (zone, south, easting, northing) of the 100 km square's south-west corner.

    Standard MGRS lettering: column letters cycle in three sets by zone, row letters in
    20-letter cycles offset by 5 in even zones; the 2 000 km row ambiguity is resolved with
    the latitude band.
    """
    text = mgrs.strip().upper().removeprefix("T")
    if len(text) != 5 or not text[:2].isdigit():
        raise ValueError(f"Not an MGRS 100 km square id: {mgrs!r}")
    zone, band, col, row = int(text[:2]), text[2], text[3], text[4]
    if not 1 <= zone <= 60 or band not in _BANDS:
        raise ValueError(f"Bad MGRS zone/band in {mgrs!r}")
    cols = _COL_SETS[(zone - 1) % 3]
    if col not in cols or row not in _ROWS:
        raise ValueError(f"Bad MGRS square letters in {mgrs!r}")
    south = band < "N"
    easting = (cols.index(col) + 1) * 100_000.0
    base = ((_ROWS.index(row) - (5 if zone % 2 == 0 else 0)) % 20) * 100_000.0
    band_lat = -80.0 + 8 * _BANDS.index(band)
    _, band_min_n = latlon_to_utm(band_lat, zone * 6 - 183, zone, south)
    # The square may straddle the band's southern edge, so it can start up to 100 km below it.
    k = math.ceil((band_min_n - 100_000.0 - base) / 2_000_000.0)
    return zone, south, easting, base + k * 2_000_000.0


def s2_grid(mgrs: str, origins: dict[str, S2TileGrid] | None = None) -> S2TileGrid:
    """Grid for any Sentinel-2 tile: supplied (metadata) origin > registered > derived from MGRS."""
    key = mgrs.strip().upper().removeprefix("T")
    if origins and key in origins:
        return origins[key]
    if key in S2_TILE_GRID:
        return S2_TILE_GRID[key]
    zone, south, e, n = mgrs_square_sw(key)
    return S2TileGrid(key, zone, e, n + 100_000.0, south=south, origin_source="derived")


def parse_tile_metadata(xml_text: str, mgrs: str) -> S2TileGrid:
    """Exact tile origin from a Sentinel-2 ``MTD_TL.xml`` (10 m Geoposition + EPSG code)."""
    root = ET.fromstring(xml_text)

    def find(tag: str) -> list[ET.Element]:
        return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == tag]

    codes = [(e.text or "").strip().upper() for e in find("HORIZONTAL_CS_CODE")]
    if not codes or not codes[0].startswith("EPSG:32"):
        raise ValueError("MTD_TL.xml has no UTM HORIZONTAL_CS_CODE (EPSG:326zz / 327zz)")
    epsg = int(codes[0].split(":")[1])
    geo = next((g for g in find("Geoposition") if g.get("resolution") == "10"), None)
    if geo is None:
        raise ValueError("MTD_TL.xml has no 10 m Geoposition")
    vals = {c.tag.rsplit("}", 1)[-1]: float(c.text or "nan") for c in geo}
    return S2TileGrid(mgrs.strip().upper().removeprefix("T"), epsg % 100, vals["ULX"], vals["ULY"],
                      south=epsg // 100 == 327, origin_source="metadata")
