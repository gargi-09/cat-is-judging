"""Smoke test for an *installed* cat-is-judging, run from outside the repository.

Checks what unit tests can't, because they run from the source tree: that the wheel
carries its data files, that nothing is written into site-packages, and that the
auditor and the maritime pipeline work end to end from the installed package.
Run it with the interpreter of a clean environment the wheel was installed into.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import sysconfig
import tempfile
from pathlib import Path


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f': {detail}' if detail else ''}")
    if not ok:
        sys.exit(1)


work = Path(tempfile.mkdtemp(prefix="cij-smoke-"))
os.environ["CAT_IS_JUDGING_DATA"] = str(work / "data")  # must be set before the app package is imported

import cat_is_judging  # noqa: E402
from cat_is_judging import Claim, FaithfulnessAuditor, Guardrail, Report, rule  # noqa: E402

site = Path(sysconfig.get_paths()["purelib"]).resolve()
check("imported from the installed package, not the source tree",
      Path(cat_is_judging.__file__).resolve().is_relative_to(site), cat_is_judging.__file__)
check("version", cat_is_judging.__version__ not in ("", "0.0.0+source"), cat_is_judging.__version__)


@rule("capital")
def capital(c: Claim, gt: dict) -> tuple:
    if gt.get(c.subject) is None:
        return "unverifiable", "no record"
    return ("supported" if gt[c.subject] == c.attributes["city"] else "contradicted"), f"record says {gt[c.subject]}"


claims = (Claim(id="fr", kind="capital", subject="France", attributes={"city": "Paris"}),
          Claim(id="jp", kind="capital", subject="Japan", attributes={"city": "Kyoto"}))
d = Guardrail(FaithfulnessAuditor([capital])).review(Report(id="r", claims=claims), {"France": "Paris", "Japan": "Tokyo"})
check("core auditor amends a report with one false claim", d.action == "amend" and [c.id for c in d.removed] == ["jp"])

from applications.maritime_threat_detection import DATA_ROOT, DATASETS_DIR  # noqa: E402
from applications.maritime_threat_detection.db import apply_schema  # noqa: E402

check("data directory honours CAT_IS_JUDGING_DATA", DATA_ROOT == (work / "data").resolve(), str(DATA_ROOT))
check("data directory is outside site-packages", not DATA_ROOT.is_relative_to(site))
conn = sqlite3.connect(":memory:")
apply_schema(conn)
check("schema.sql shipped and applies", conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 5)
manifests = sorted(p.name for p in DATASETS_DIR.glob("*.json"))
check("example manifests shipped", "example_georeferenced.json" in manifests, ", ".join(manifests))

# End to end: a tiny georeferenced dataset with one unlit hull, built and reconciled.
import pandas as pd  # noqa: E402

from applications.maritime_threat_detection.dataset import load_manifest  # noqa: E402
from applications.maritime_threat_detection.scripts.ingest_telemetry import run_manifest  # noqa: E402

ships = [(538000101, 25.10, 56.10), (538000102, 25.12, 56.20), (538000103, 25.08, 56.30)]
pd.DataFrame([{"MMSI": m, "Time": f"2026-03-03 {h:02d}:{mm:02d}:00", "Lat": la, "Lon": lo, "SOG": 0.0, "Length": 150}
              for m, la, lo in ships for h in range(8, 13) for mm in (0, 30)]).to_csv(work / "ais.csv", index=False)
pd.DataFrame([{"scene": "S1", "lat": la, "lon": lo, "len": 150, "label": m} for m, la, lo in ships]
             + [{"scene": "S1", "lat": 25.15, "lon": 56.35, "len": 90, "label": ""}]).to_csv(work / "det.csv", index=False)
pd.DataFrame([{"scene_id": "S1", "acquired_at": "2026-03-03T10:00:00Z", "lat_min": 25.0, "lat_max": 25.2,
               "lon_min": 56.0, "lon_max": 56.4}]).to_csv(work / "scenes.csv", index=False)
(work / "dataset.json").write_text(json.dumps({
    "name": "smoke", "label_provenance": "independent",
    "ais": {"path": "ais.csv", "utc_offset": "+00:00",
            "columns": {"mmsi": "MMSI", "postime": "Time", "lat": "Lat", "lon": "Lon", "sog": "SOG", "length": "Length"}},
    "imagery": {"format": "georeferenced_csv", "annotations": "det.csv", "scenes": "scenes.csv",
                "columns": {"scene_id": "scene", "length_m": "len"}},
}))
report = run_manifest(load_manifest(work / "dataset.json"))
b = report["cross_reference"]["blind_association"]
check("dataset builds into the data directory", Path(report["database"]).resolve().is_relative_to(DATA_ROOT), report["database"])
check("pipeline matches the AIS ships and finds the unlit hull",
      b["precision"] == 1.0 and b["dark_vessel_recall"] == 1.0, json.dumps({k: b[k] for k in ("precision", "dark_vessel_recall")}))
print("smoke test passed")
