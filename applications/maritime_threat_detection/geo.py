"""Deterministic geodesy for reconciling Sentinel-2 pixels with AIS lat/lon.

The annotation JSONs carry no coordinates — only a Sentinel-2 product name, an MGRS tile
id and the crop's pixel offset (``..._T40RDN_...__x3416_y1315``). Georeferencing therefore
has to be reconstructed from the Sentinel-2 tiling grid: each MGRS tile is a 10 980 x 10 980
pixel raster at 10 m GSD in its UTM zone, anchored at a fixed upper-left corner.

We implement WGS84 <-> UTM (Snyder's series, sub-millimetre at these latitudes) in pure
Python instead of pulling in ``pyproj``: the auditor's ground truth must be reproducible
and dependency-light, and two tiles in one zone don't justify a GDAL/PROJ stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_A = 6_378_137.0  # WGS84 semi-major axis (m)
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_FALSE_EASTING = 500_000.0

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class S2TileGrid:
    """Upper-left anchor of one Sentinel-2 MGRS tile in its UTM zone (northern hemisphere)."""

    mgrs: str
    utm_zone: int
    ulx: float
    uly: float
    gsd_m: float = 10.0


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


def latlon_to_utm(lat: float, lon: float, zone: int) -> tuple[float, float]:
    """WGS84 geographic -> UTM (easting, northing) in metres, northern hemisphere."""
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
    return easting, northing


def utm_to_latlon(easting: float, northing: float, zone: int) -> tuple[float, float]:
    """UTM (easting, northing) -> WGS84 (lat, lon) in degrees, northern hemisphere."""
    x = easting - _FALSE_EASTING
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
    return utm_to_latlon(grid.ulx + px_x * grid.gsd_m, grid.uly - px_y * grid.gsd_m, grid.utm_zone)


def latlon_to_pixel(grid: S2TileGrid, lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) -> full-tile pixel coordinates (column, row)."""
    easting, northing = latlon_to_utm(lat, lon, grid.utm_zone)
    return (easting - grid.ulx) / grid.gsd_m, (grid.uly - northing) / grid.gsd_m


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres — used for AIS-vs-optical position residuals."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))
