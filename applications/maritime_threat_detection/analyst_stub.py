"""Stand-in analysts that write tile reports for the auditor to judge.

Phase 3 replaces these with a real analyst component. Until then, three rule-based
styles produce the failure modes the auditor has to handle, from the same ground truth:

* ``ais_trusting``  — reports every AIS ship in the footprint, where AIS says it is, and
  "no dark vessels". It repeats spoofed positions and ghost signals faithfully, which is
  the point: it is confidently, consistently wrong.
* ``imagery_first`` — reports what the satellite saw, with identities from blind
  (label-free) association. Never reads the annotator labels.
* ``alarmist``      — the "rule that cried wolf": AIS-trusting, plus a going-dark
  accusation for every ship whose AIS lapses near the tile, and every unresolved hull
  counted as dark.

Sampling mirrors how a language-model analyst is usually audited: draw 0 is the
deterministic (greedy) report, draws 1..n are stochastic re-generations used only for
consistency. Each draw has its own seeded RNG, so everything is reproducible. The noise
models a generator that is unsure: claims are sometimes omitted, positions jitter, counts
are off by one. These are simulation settings, not thresholds.
"""

from __future__ import annotations

import math
import random
from typing import Literal

from cat_is_judging import Claim, Report
from applications.maritime_threat_detection import audit as A
from applications.maritime_threat_detection.geo import EARTH_RADIUS_M
from applications.maritime_threat_detection.reconciliation import inside_chip

Style = Literal["ais_trusting", "imagery_first", "alarmist"]
STYLES: tuple[str, ...] = ("ais_trusting", "imagery_first", "alarmist")

SAMPLE_OMISSION_P: float = 0.15   # chance a draw leaves out a vessel_present claim
SAMPLE_JITTER_M: float = 120.0    # 1-sigma position noise per draw
SAMPLE_MISCOUNT_P: float = 0.15   # chance a draw's vessel count is off by one
_M_PER_DEG = math.radians(1) * EARTH_RADIUS_M


def _jitter(rng: random.Random, lat: float, lon: float) -> tuple[float, float]:
    dn, de = rng.gauss(0, SAMPLE_JITTER_M), rng.gauss(0, SAMPLE_JITTER_M)
    return lat + dn / _M_PER_DEG, lon + de / (_M_PER_DEG * math.cos(math.radians(lat)))


def _ais_view(gt: A.MaritimeGroundTruth, tile_id: str) -> list[tuple[int, float, float]]:
    t = gt.tile(tile_id)
    out = []
    for m in sorted(gt.ais.registry):
        p = gt.ais.position(m, t["acquired_epoch"])
        if p is not None and inside_chip(t, p.lat, p.lon):
            out.append((m, p.lat, p.lon))
    return out


def draft_report(gt: A.MaritimeGroundTruth, tile_id: str, style: Style = "ais_trusting", draw: int = 0, seed: int = 7) -> Report:
    """One report for ``tile_id``. ``draw`` 0 is the report; 1..n are consistency samples."""
    t = gt.tile(tile_id)
    if t is None:
        raise ValueError(f"unknown tile {tile_id!r}")
    rng = random.Random(f"{seed}:{style}:{tile_id}:{draw}")
    noisy = draw > 0
    claims: list[Claim] = []
    results, _ = gt.blind(tile_id)

    if style == "imagery_first":
        dets = gt.detections(tile_id)
        ships = [(r.assignment.mmsi, d["center_lat"], d["center_lon"])
                 for d, r in zip(dets, results) if r.assignment.mmsi is not None]
        count = len(dets)
        dark = sum(r.status == "dark_vessel" for r in results)
    else:
        ships = _ais_view(gt, tile_id)
        count, dark = len(ships), 0

    if noisy and rng.random() < SAMPLE_MISCOUNT_P:
        count = max(0, count + rng.choice((-1, 1)))
    claims.append(A.vessel_count(tile_id, count))
    for m, lat, lon in ships:
        if noisy and rng.random() < SAMPLE_OMISSION_P:
            continue
        claims.append(A.vessel_present(tile_id, m, *(_jitter(rng, lat, lon) if noisy else (lat, lon))))

    if style == "alarmist":
        near_gap = sorted({m for r in results if r.status == "unresolved_gap" for m in r.near_gap_mmsis})
        dark = sum(r.status in ("dark_vessel", "unresolved_gap") for r in results)
        claims += [A.going_dark(tile_id, m) for m in near_gap]
    claims.append(A.dark_vessel_count(tile_id, dark))
    return Report(id=f"{tile_id}/{style}/draw{draw}", claims=tuple(claims), source=f"analyst_stub:{style}")

