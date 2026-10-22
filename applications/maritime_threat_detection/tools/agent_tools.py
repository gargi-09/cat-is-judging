"""Typed, read-only tools the analyst agent investigates a tile with.

Design rules:

* **Read-only.** Every tool reads the ground-truth store through ``MaritimeGroundTruth``
  (opened read-only); nothing the analyst does can change what the auditor checks against.
* **No annotator labels.** Hulls are listed by index, position, size and orientation only.
  The labels were made by matching to AIS; handing them to the analyst would be circular.
* **Measurements, not verdicts, where the analyst should do the reasoning.** Geodesy is
  done for it (an LLM shouldn't eyeball haversine), but whether a gap means "spoofing"
  is the analyst's call, and the auditor's to check.

Each tool is a strict JSON schema plus a function ``(gt, args) -> dict``. Errors are raised
as ``ToolError`` and returned to the model as ``is_error`` results, never swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from applications.maritime_threat_detection.audit import MaritimeGroundTruth
from applications.maritime_threat_detection.geo import haversine_m
from applications.maritime_threat_detection.reconciliation import (
    _axis_diff_deg,
    assess_silence,
    inside_chip,
    tile_candidates,
    tolerance_m,
    validate_mmsi,
)

TRACK_WINDOW_S: int = 3 * 3600  # fixes shown either side of the image time
TRACK_MAX_FIXES: int = 40


class ToolError(ValueError):
    """Bad tool input (unknown tile, hull or MMSI): reported to the model, which can retry."""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    properties: dict[str, Any]
    run: Callable[[MaritimeGroundTruth, dict[str, Any]], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "strict": True,
                "input_schema": {"type": "object", "properties": self.properties,
                                 "required": list(self.properties), "additionalProperties": False}}


def _tile(gt: MaritimeGroundTruth, tile_id: str):
    t = gt.tile(tile_id)
    if t is None:
        raise ToolError(f"unknown tile {tile_id!r}")
    return t


def _hull(gt: MaritimeGroundTruth, tile_id: str, index: int):
    dets = gt.detections(tile_id)
    if not 0 <= index < len(dets):
        raise ToolError(f"tile {tile_id} has hulls 0..{len(dets) - 1}, not {index}")
    return dets[index]


def get_tile(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    return {"tile_id": t["tile_id"], "acquired_at_utc": t["acquired_at"],
            "footprint": {"lat_min": t["lat_min"], "lat_max": t["lat_max"], "lon_min": t["lon_min"], "lon_max": t["lon_max"]},
            "hulls_detected": len(gt.detections(t["tile_id"])),
            "pixel_size_m": t["gsd_m"]}


def list_hulls(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    return {"tile_id": t["tile_id"], "hulls": [
        {"hull": i, "lat": round(d["center_lat"], 6), "lon": round(d["center_lon"], 6), "length_m": round(d["length_m"], 1),
         "axis_bearing_deg": None if d["axis_bearing_deg"] is None else round(d["axis_bearing_deg"], 1)}
        for i, d in enumerate(gt.detections(t["tile_id"]))]}


def ais_near_tile(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    out = []
    for p in tile_candidates(t, gt.ais):
        out.append({"mmsi": p.mmsi, "name": gt.ais.name.get(p.mmsi), "lat": round(p.lat, 6), "lon": round(p.lon, 6),
                    "estimate_method": p.method, "nearest_fix_s": p.fix_age_s, "uncertainty_m": p.uncertainty_m,
                    "sog_kn": p.sog_kn, "course_deg": p.course_deg, "ais_length_m": gt.ais.length_m.get(p.mmsi),
                    "inside_footprint": inside_chip(t, p.lat, p.lon)})
    return {"tile_id": t["tile_id"], "image_time_utc": t["acquired_at"],
            "note": "AIS positions estimated at the image time; ships AIS can't place then are not listed.",
            "vessels": out}


def ais_track(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    m = a["mmsi"]
    if m not in gt.ais.registry:
        raise ToolError(f"MMSI {m} is not in this AIS feed")
    e = t["acquired_epoch"]
    rows = gt.conn.execute(
        "SELECT postime, postime_epoch, lat, lon, sog, cog, hdg, nav_status FROM ais_positions WHERE mmsi = ? "
        "AND postime_epoch BETWEEN ? AND ? ORDER BY ABS(postime_epoch - ?) LIMIT ?",
        (m, e - TRACK_WINDOW_S, e + TRACK_WINDOW_S, e, TRACK_MAX_FIXES)).fetchall()
    fixes = sorted(({"time_utc": r["postime"], "minutes_from_image": round((r["postime_epoch"] - e) / 60, 1),
                     "lat": r["lat"], "lon": r["lon"], "sog_kn": r["sog"], "cog_deg": r["cog"], "heading_deg": r["hdg"],
                     "nav_status": r["nav_status"]} for r in rows), key=lambda f: f["minutes_from_image"])
    g = gt.ais.gap(m, e)
    return {"mmsi": m, "name": gt.ais.name.get(m), "image_time_utc": t["acquired_at"],
            "fixes_within_3h": fixes,
            "gap_at_image_time": {"previous_fix_min_before": None if g.prev_epoch is None else round((e - g.prev_epoch) / 60, 1),
                                  "next_fix_min_after": None if g.next_epoch is None else round((g.next_epoch - e) / 60, 1),
                                  "distance_between_them_m": None if g.displacement_m is None else round(g.displacement_m)}}


def compare_hull_to_ais(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    h = _hull(gt, t["tile_id"], a["hull"])
    m = a["mmsi"]
    if m not in gt.ais.registry:
        raise ToolError(f"MMSI {m} is not in this AIS feed")
    p = gt.ais.position(m, t["acquired_epoch"])
    if p is None:
        return {"hull": a["hull"], "mmsi": m, "ais_position_at_image_time": None,
                "note": "AIS can't place this ship at the image time; see ais_silence for why."}
    d, tol = haversine_m(h["center_lat"], h["center_lon"], p.lat, p.lon), tolerance_m(p)
    ais_len = gt.ais.length_m.get(m)
    return {"hull": a["hull"], "mmsi": m, "distance_m": round(d), "tolerance_m": round(tol), "within_tolerance": d <= tol,
            "tolerance_rule": "500 m plus the AIS estimate's own uncertainty", "estimate_method": p.method,
            "hull_length_m": round(h["length_m"], 1), "ais_length_m": ais_len,
            "axis_vs_course_deg": (None if p.course_deg is None or h["axis_bearing_deg"] is None
                                   else round(_axis_diff_deg(h["axis_bearing_deg"], p.course_deg), 1))}


def check_mmsi(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    m = a["mmsi"]
    bad = validate_mmsi(m)
    return {"mmsi": m, "valid_ship_mmsi": bad is None, "problem": bad, "in_this_ais_feed": m in gt.ais.registry,
            "name": gt.ais.name.get(m), "ais_length_m": gt.ais.length_m.get(m),
            "note": "Not being in this feed is not the same as being unregistered."}


def ais_silence(gt: MaritimeGroundTruth, a: dict[str, Any]) -> dict[str, Any]:
    t = _tile(gt, a["tile_id"])
    m = a["mmsi"]
    if m not in gt.ais.registry:
        raise ToolError(f"MMSI {m} is not in this AIS feed")
    p = gt.ais.position(m, t["acquired_epoch"])
    if p is not None:
        return {"mmsi": m, "silent_at_image_time": False, "estimate_method": p.method, "nearest_fix_s": p.fix_age_s}
    v = assess_silence(m, t["acquired_epoch"], gt.ais)
    return {"mmsi": m, "silent_at_image_time": True, "assessment": v.verdict, "reason": v.reason,
            "rule": "Going dark needs >= 12 h of silence while most of the fleet is still heard (Global Fishing Watch). "
                    "Shorter gaps are reception gaps: insufficient data, not evidence of evasion."}


_TILE = {"tile_id": {"type": "string", "description": "Tile id, e.g. T40RDP_20260303_x4909_y9260"}}
_MMSI = {"mmsi": {"type": "integer", "description": "9-digit MMSI"}}
_HULL = {"hull": {"type": "integer", "description": "Hull index from list_hulls"}}

TOOLS: list[AgentTool] = [
    AgentTool("get_tile", "Acquisition time, footprint and number of hulls the satellite detected in a tile.", _TILE, get_tile),
    AgentTool("list_hulls", "Every hull the satellite saw in the tile: index, position, length and long-axis bearing "
              "(bow and stern are indistinguishable). Hulls carry no identity.", _TILE, list_hulls),
    AgentTool("ais_near_tile", "AIS-reported vessels near the tile, with each one's position estimated at the image "
              "time, how it was estimated and how uncertain it is.", _TILE, ais_near_tile),
    AgentTool("ais_track", "One vessel's raw AIS fixes within 3 h of the tile's image time, and the gap around it.",
              _TILE | _MMSI, ais_track),
    AgentTool("compare_hull_to_ais", "Distance between one hull and one vessel's AIS position at the image time, the "
              "matching tolerance, and length / orientation agreement.", _TILE | _HULL | _MMSI, compare_hull_to_ais),
    AgentTool("check_mmsi", "Is an MMSI structurally a ship identity (ITU rules), and is it in this AIS feed?", _MMSI, check_mmsi),
    AgentTool("ais_silence", "Why AIS can't place a vessel at the image time: a reception gap, or a potential going-dark "
              "candidate under the 12 h rule.", _TILE | _MMSI, ais_silence),
]
TOOLS_BY_NAME: dict[str, AgentTool] = {t.name: t for t in TOOLS}


def run_tool(gt: MaritimeGroundTruth, name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"no tool named {name!r}")
    missing = [k for k in tool.properties if k not in args]
    if missing:
        raise ToolError(f"missing argument(s) {missing}")
    return tool.run(gt, args)
