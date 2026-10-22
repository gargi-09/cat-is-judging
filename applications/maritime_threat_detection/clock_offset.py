"""Find the AIS clock's timezone without trusting labels or column hints.

The bundled data's timestamps were naive, and another column said UTC+8. The obvious
reading was wrong: UTC was right, and UTC+8 would have flagged ordinary ships as spoofers.
That was settled by sweeping offsets against the *labels*. Labels aren't always available
(and are sometimes made from AIS), so this sweep is label-free.

Only moving ships carry evidence about the clock. An anchored ship is in the same place at
every offset, so counting "hulls that found a track" mostly measures how complete AIS
reception was at each candidate time. On the bundled data that naive metric picked UTC-2,
which is wrong: several ships sat at anchor on their hulls until ~05:20, then their AIS
jumped tens of km at implausible reported speeds (33-48 kn) while the satellite still saw
them at anchor at 06:47. Querying two hours early simply put them back on their hulls.

So the sweep counts, per offset, the *moving* AIS tracks that land on a hull. An hour off,
a 2 kn ship is 3.7 km away. And it refuses to guess: with too few moving tracks on hulls,
or a winner that isn't two standard errors clear of the runner-up, it
reports "ambiguous" and the ETL keeps UTC, flagged in the lineage record. On the bundled
data (mostly anchored ships, ~25 moving tracks) it does exactly that.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from applications.maritime_threat_detection.reconciliation import AISLookup, associate_tile, inside_chip

# Whole-hour offsets cover every civil timezone's whole-hour part (UTC-12 .. UTC+14).
CANDIDATE_OFFSETS_H: tuple[int, ...] = tuple(range(-12, 15))
SWEEP_MAX_TILES: int = 200  # spread evenly over the dataset
# At 2 kn a one-hour clock error moves a ship 3.7 km, far beyond the 500 m tolerance.
MOVING_MIN_SOG_KN: float = 2.0
MIN_MOVING_TRACKS: int = 10  # fewer moving tracks on hulls than this and the sweep won't decide
# The winner must beat the runner-up by this many standard errors of the difference
# (two proportions over all hulls, +1/+2 smoothing so small counts aren't "certain").
AUTO_OFFSET_MIN_Z: float = 2.0


@dataclass
class SweepResult:
    offset_h: int | None  # chosen offset, None when ambiguous
    table: list[dict[str, Any]]
    reason: str

    @property
    def offset_text(self) -> str | None:
        return None if self.offset_h is None else f"{'+' if self.offset_h >= 0 else '-'}{abs(self.offset_h):02d}:00"


def sweep(conn: sqlite3.Connection, offsets: tuple[int, ...] = CANDIDATE_OFFSETS_H,
          max_tiles: int = SWEEP_MAX_TILES) -> SweepResult:
    """``conn`` holds AIS loaded as if its naive timestamps were UTC.

    If the source clock is really UTC+h, a fix stored at epoch e happened at e - h, so the
    AIS matching an image taken at T is found by querying at T + h.
    """
    tiles = conn.execute("SELECT * FROM optical_tiles WHERE n_detections > 0 ORDER BY tile_id").fetchall()
    if len(tiles) > max_tiles:
        step = len(tiles) / max_tiles
        tiles = [tiles[int(i * step)] for i in range(max_tiles)]
    dets = {t["tile_id"]: conn.execute("SELECT * FROM optical_detections WHERE tile_id = ?", (t["tile_id"],)).fetchall()
            for t in tiles}
    hulls = sum(len(v) for v in dets.values())
    if not hulls:
        return SweepResult(None, [], "no detections to sweep against")
    ais = AISLookup(conn)
    table = []
    for h in offsets:
        moving = landed = 0
        for t in tiles:
            shifted = dict(t) | {"acquired_epoch": t["acquired_epoch"] + h * 3600}
            results, _ = associate_tile(shifted, dets[t["tile_id"]], ais)
            matched = {r.assignment.mmsi for r in results if r.status == "matched"}
            for m in ais.registry:
                p = ais.position(m, shifted["acquired_epoch"])
                if p is not None and (p.sog_kn or 0) >= MOVING_MIN_SOG_KN and inside_chip(shifted, p.lat, p.lon):
                    moving += 1
                    landed += m in matched
        # Fixed denominator (all hulls): a wrong offset that drifts moving ships out of the
        # chips scores low, rather than looking merely "uncertain".
        table.append({"offset_h": h, "moving_tracks": moving, "on_a_hull": landed, "hulls": hulls,
                      "share": round(landed / hulls, 4)})
    ranked = sorted(table, key=lambda r: (-r["on_a_hull"], abs(r["offset_h"])))
    best, second = ranked[0], ranked[1]
    if best["on_a_hull"] < MIN_MOVING_TRACKS:
        return SweepResult(None, table, f"ambiguous: at best {best['on_a_hull']} moving AIS tracks land on a hull "
                                        f"(need {MIN_MOVING_TRACKS}); stationary ships say nothing about the clock")
    z = _z(best["on_a_hull"], second["on_a_hull"], hulls)
    if z < AUTO_OFFSET_MIN_Z:
        return SweepResult(None, table, f"ambiguous: UTC{best['offset_h']:+d} h ({best['on_a_hull']} moving tracks on "
                                        f"hulls) vs UTC{second['offset_h']:+d} h ({second['on_a_hull']}) is within noise "
                                        f"(z = {z:.1f} < {AUTO_OFFSET_MIN_Z})")
    return SweepResult(best["offset_h"], table,
                       f"UTC{best['offset_h']:+d} h: {best['on_a_hull']} moving tracks land on a hull, clear of "
                       f"UTC{second['offset_h']:+d} h ({second['on_a_hull']}, z = {z:.1f})")


def _z(k1: int, k2: int, n: int) -> float:
    """Two-proportion z for k1/n vs k2/n, with +1/+2 smoothing so small counts aren't 'certain'."""
    p1, p2 = (k1 + 1) / (n + 2), (k2 + 1) / (n + 2)
    return (p1 - p2) / math.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / (n + 2))
