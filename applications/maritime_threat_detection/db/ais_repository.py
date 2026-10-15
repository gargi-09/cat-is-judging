"""Spatiotemporal AIS lookups against the ground-truth store.

Phase 1 needs exactly one primitive — "where did this MMSI's AIS say it was at time t" —
to cross-reference optical detections at ingest time. Agent-facing queries build on it.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel

from applications.maritime_threat_detection.geo import haversine_m

# AIS class A reports every 2-10 s underway and every 3 min at anchor; providers decimate.
# Interpolating across a gap longer than an hour would invent a track we never observed.
MAX_INTERPOLATION_GAP_S: int = 3600
# Without a bracketing pair, accept the nearest single fix only if it is this fresh.
MAX_NEAREST_FIX_AGE_S: int = 900
# Providers de-duplicate unchanged positions, so ships at anchor show multi-hour gaps. If the
# fixes either side of a long gap are this close, the ship held station and the gap is not
# silence. Bounded so a vessel that left and came back within a day is not "held".
STATIONARY_HOLD_RADIUS_M: float = 300.0
MAX_STATIONARY_HOLD_GAP_S: int = 12 * 3600


class TrackPosition(BaseModel):
    mmsi: int
    epoch: int
    lat: float
    lon: float
    method: Literal["exact", "interpolated", "stationary_hold", "nearest"]
    fix_age_s: int  # time to the closest real AIS fix used — a staleness bound on the estimate


def position_at(conn: sqlite3.Connection, mmsi: int, epoch: int) -> TrackPosition | None:
    """Estimate a vessel's AIS-reported position at ``epoch``; None if AIS is silent then.

    Linear interpolation in lat/lon is adequate here: fixes are minutes apart, so the
    great-circle vs rhumb-line difference is centimetres.
    """
    prev = conn.execute(
        "SELECT postime_epoch, lat, lon FROM ais_positions "
        "WHERE mmsi = ? AND postime_epoch <= ? ORDER BY postime_epoch DESC LIMIT 1",
        (mmsi, epoch),
    ).fetchone()
    nxt = conn.execute(
        "SELECT postime_epoch, lat, lon FROM ais_positions "
        "WHERE mmsi = ? AND postime_epoch >= ? ORDER BY postime_epoch ASC LIMIT 1",
        (mmsi, epoch),
    ).fetchone()

    if prev is not None and prev[0] == epoch:
        return TrackPosition(mmsi=mmsi, epoch=epoch, lat=prev[1], lon=prev[2], method="exact", fix_age_s=0)
    if prev is not None and nxt is not None:
        gap = nxt[0] - prev[0]
        held = gap <= MAX_STATIONARY_HOLD_GAP_S and haversine_m(prev[1], prev[2], nxt[1], nxt[2]) <= STATIONARY_HOLD_RADIUS_M
        if gap <= MAX_INTERPOLATION_GAP_S or held:
            w = (epoch - prev[0]) / gap
            return TrackPosition(
                mmsi=mmsi,
                epoch=epoch,
                lat=prev[1] + w * (nxt[1] - prev[1]),
                lon=prev[2] + w * (nxt[2] - prev[2]),
                method="interpolated" if gap <= MAX_INTERPOLATION_GAP_S else "stationary_hold",
                fix_age_s=int(min(epoch - prev[0], nxt[0] - epoch)),
            )
    candidates = [r for r in (prev, nxt) if r is not None and abs(r[0] - epoch) <= MAX_NEAREST_FIX_AGE_S]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs(r[0] - epoch))
    return TrackPosition(
        mmsi=mmsi, epoch=epoch, lat=best[1], lon=best[2], method="nearest", fix_age_s=int(abs(best[0] - epoch))
    )
