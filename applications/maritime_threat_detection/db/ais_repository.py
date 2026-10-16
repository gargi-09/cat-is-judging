"""Spatiotemporal AIS lookups against the ground-truth store.

The core primitive is "where did this MMSI's AIS say it was at time t, and how sure can we
be". Every estimate carries an explicit ``uncertainty_m`` so downstream rules can widen
their tolerance for stale or extrapolated positions instead of using one fixed radius —
a 15-minute-old fix on a ship doing 14 kn is 6 km stale, a 15-minute-old fix on a ship at
anchor is not.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Literal

from pydantic import BaseModel

from applications.maritime_threat_detection.geo import EARTH_RADIUS_M, haversine_m

KNOT_MPS: float = 0.514444

# AIS class A reports every 2-10 s underway and every 3 min at anchor; providers decimate.
# Interpolating across a gap longer than an hour would invent a track we never observed.
MAX_INTERPOLATION_GAP_S: int = 3600
# Providers de-duplicate unchanged positions, so ships at anchor show multi-hour gaps. If the
# fixes either side of a long gap are this close, the ship held station and the gap is not
# silence. Bounded so a vessel that left and came back within a day is not "held".
STATIONARY_HOLD_RADIUS_M: float = 300.0
MAX_STATIONARY_HOLD_GAP_S: int = 12 * 3600
# With only one usable fix, a moving ship is dead-reckoned from its reported speed and
# course (Sentinel-2 snapshots are instantaneous; the fix is not). Beyond 30 min, course
# changes make the extrapolation more invention than estimate.
MAX_DEAD_RECKON_AGE_S: int = 1800
MIN_DEAD_RECKON_SOG_KN: float = 1.0
# A single fix on a (near-)stationary ship is still usable if it is this fresh.
MAX_NEAREST_FIX_AGE_S: int = 900

# Uncertainty model, in metres. Deliberately simple and conservative; each term is the
# plausible error of that estimation method, not a statistical fit.
INTERPOLATION_ERROR_FRACTION: float = 0.10  # straight line vs a curving track between fixes
DEAD_RECKON_ERROR_FRACTION: float = 0.20    # speed/course drift over the extrapolated distance
STATIONARY_HOLD_ERROR_M: float = STATIONARY_HOLD_RADIUS_M / 2
# Across a long reception gap the straight line between fixes is only a rough corridor.
# Used solely as a low-confidence fallback for blind association, never for corroboration.
MAX_GAP_BRIDGE_S: int = 12 * 3600
GAP_BRIDGE_ERROR_FRACTION: float = 0.25

Method = Literal["exact", "interpolated", "stationary_hold", "dead_reckoned", "nearest", "gap_bridged"]


class TrackPosition(BaseModel):
    mmsi: int
    epoch: int
    lat: float
    lon: float
    method: Method
    fix_age_s: int  # time to the closest real AIS fix used
    uncertainty_m: float  # plausible error of this estimate, added to matching tolerances
    sog_kn: float | None = None
    course_deg: float | None = None  # true heading if reported, else course over ground


class TrackGap(BaseModel):
    """The AIS fixes either side of a time — used to tell 'no data' from 'went dark'."""

    prev_epoch: int | None
    next_epoch: int | None
    gap_s: int | None  # next - prev; None when the track is open-ended on one side
    displacement_m: float | None  # distance between the bracketing fixes


_FIX_SQL = "SELECT postime_epoch, lat, lon, sog, cog, hdg FROM ais_positions WHERE mmsi = ? AND postime_epoch {op} ? ORDER BY postime_epoch {order} LIMIT 1"


def _bracket(conn: sqlite3.Connection, mmsi: int, epoch: int) -> tuple[sqlite3.Row | tuple | None, ...]:
    prev = conn.execute(_FIX_SQL.format(op="<=", order="DESC"), (mmsi, epoch)).fetchone()
    nxt = conn.execute(_FIX_SQL.format(op=">=", order="ASC"), (mmsi, epoch)).fetchone()
    return prev, nxt


def _course(fix: sqlite3.Row | tuple) -> float | None:
    hdg, cog = fix[5], fix[4]
    return hdg if hdg is not None else cog


def _dead_reckon(lat: float, lon: float, sog_kn: float, cog_deg: float, dt_s: float) -> tuple[float, float]:
    """Flat-earth propagation; exact to metres over the <= 30 min we allow."""
    d = sog_kn * KNOT_MPS * dt_s
    c = math.radians(cog_deg)
    m_per_deg = math.radians(1) * EARTH_RADIUS_M  # same sphere as haversine_m, so they agree
    dlat = d * math.cos(c) / m_per_deg
    dlon = d * math.sin(c) / (m_per_deg * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def position_at(conn: sqlite3.Connection, mmsi: int, epoch: int) -> TrackPosition | None:
    """Estimate a vessel's AIS-reported position at ``epoch``; None if AIS can't place it.

    Order of preference: exact fix > interpolation between bracketing fixes > stationary
    hold > dead reckoning from one fix > nearest fix on a stationary ship > None.
    """
    prev, nxt = _bracket(conn, mmsi, epoch)

    def make(lat: float, lon: float, method: Method, age: float, unc: float, fix: tuple) -> TrackPosition:
        return TrackPosition(mmsi=mmsi, epoch=epoch, lat=lat, lon=lon, method=method, fix_age_s=int(round(age)),
                             uncertainty_m=round(unc, 1), sog_kn=fix[3], course_deg=_course(fix))

    if prev is not None and prev[0] == epoch:
        return make(prev[1], prev[2], "exact", 0, 0.0, prev)

    if prev is not None and nxt is not None:
        gap = nxt[0] - prev[0]
        seg = haversine_m(prev[1], prev[2], nxt[1], nxt[2])
        if gap <= MAX_INTERPOLATION_GAP_S or (gap <= MAX_STATIONARY_HOLD_GAP_S and seg <= STATIONARY_HOLD_RADIUS_M):
            w = (epoch - prev[0]) / gap
            interpolated = gap <= MAX_INTERPOLATION_GAP_S
            return make(
                prev[1] + w * (nxt[1] - prev[1]), prev[2] + w * (nxt[2] - prev[2]),
                "interpolated" if interpolated else "stationary_hold",
                min(epoch - prev[0], nxt[0] - epoch),
                INTERPOLATION_ERROR_FRACTION * seg if interpolated else STATIONARY_HOLD_ERROR_M,
                prev if epoch - prev[0] <= nxt[0] - epoch else nxt,
            )

    singles = sorted((r for r in (prev, nxt) if r is not None), key=lambda r: abs(r[0] - epoch))
    for fix in singles:
        dt = epoch - fix[0]  # negative when reckoning backwards from a later fix
        sog, cog = fix[3], fix[4]
        if abs(dt) <= MAX_DEAD_RECKON_AGE_S and sog is not None and cog is not None and sog >= MIN_DEAD_RECKON_SOG_KN:
            lat, lon = _dead_reckon(fix[1], fix[2], sog, cog, dt)
            return make(lat, lon, "dead_reckoned", abs(dt), DEAD_RECKON_ERROR_FRACTION * sog * KNOT_MPS * abs(dt), fix)
        if abs(dt) <= MAX_NEAREST_FIX_AGE_S:
            # No usable course to reckon with (or barely moving): the fix stands, and the
            # distance the ship could have covered since bounds the error.
            drift = (sog if sog is not None else MIN_DEAD_RECKON_SOG_KN) * KNOT_MPS * abs(dt)
            return make(fix[1], fix[2], "nearest", abs(dt), drift, fix)
    return None


def position_across_gap(conn: sqlite3.Connection, mmsi: int, epoch: int) -> TrackPosition | None:
    """Low-confidence corridor estimate across a reception gap of up to 12 h.

    Only for blind association's second pass: it lets a hull be tied to a ship whose AIS
    simply wasn't received at image time, instead of being called a dark vessel.
    """
    prev, nxt = _bracket(conn, mmsi, epoch)
    if prev is None or nxt is None or nxt[0] - prev[0] > MAX_GAP_BRIDGE_S or nxt[0] == prev[0]:
        return None
    w = (epoch - prev[0]) / (nxt[0] - prev[0])
    seg = haversine_m(prev[1], prev[2], nxt[1], nxt[2])
    return TrackPosition(
        mmsi=mmsi, epoch=epoch, lat=prev[1] + w * (nxt[1] - prev[1]), lon=prev[2] + w * (nxt[2] - prev[2]),
        method="gap_bridged", fix_age_s=int(min(epoch - prev[0], nxt[0] - epoch)),
        uncertainty_m=round(GAP_BRIDGE_ERROR_FRACTION * seg, 1), sog_kn=None, course_deg=None,
    )


def fleet_coverage(conn: sqlite3.Connection, start: int, end: int, exclude: int | None = None) -> float:
    """Share of known vessels with at least one fix in ``[start, end]`` — a reception proxy.

    A gap only suggests deliberate silence if the rest of the fleet was being heard.
    """
    total = conn.execute("SELECT COUNT(*) FROM vessels WHERE mmsi IS NOT ?", (exclude,)).fetchone()[0]
    heard = conn.execute(
        "SELECT COUNT(DISTINCT mmsi) FROM ais_positions WHERE postime_epoch BETWEEN ? AND ? AND mmsi IS NOT ?",
        (start, end, exclude),
    ).fetchone()[0]
    return heard / total if total else 0.0


def gap_at(conn: sqlite3.Connection, mmsi: int, epoch: int) -> TrackGap:
    prev, nxt = _bracket(conn, mmsi, epoch)
    return TrackGap(
        prev_epoch=prev[0] if prev else None,
        next_epoch=nxt[0] if nxt else None,
        gap_s=(nxt[0] - prev[0]) if prev and nxt else None,
        displacement_m=haversine_m(prev[1], prev[2], nxt[1], nxt[2]) if prev and nxt else None,
    )
