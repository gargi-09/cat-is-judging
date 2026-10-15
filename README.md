# Cat-is-Judging

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Tests](https://img.shields.io/badge/tests-29%20passing-brightgreen?logo=pytest&logoColor=white)](tests/)
[![Status](https://img.shields.io/badge/status-phase%201%20of%205-orange)](#roadmap)

Neurosymbolic faithfulness auditing for RAG systems: catches hallucinated answers by combining symbolic verification against ground truth with self-consistency scoring.

The library (`cat_is_judging/`) is domain-agnostic. It is showcased through a production-style application, **Maritime Threat & Dark Vessel Detection** (`applications/maritime_threat_detection/`), which reconciles Sentinel-2 satellite imagery against AIS transponder telemetry to find vessels that are dark, spoofing, or phantom.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Data engineering: AIS + Sentinel-2 annotations → indexed SQLite ground truth | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 2 | Core SDK: symbolic verifier, consistency scorer, fuser, guardrail interceptor | ![next](https://img.shields.io/badge/-next-blue) |
| 3 | Recon agent and typed AIS / optical tools | ![planned](https://img.shields.io/badge/-planned-lightgrey) |
| 4 | Golden eval set and CI faithfulness gate | ![planned](https://img.shields.io/badge/-planned-lightgrey) |
| 5 | CLI / FastAPI interface and case study | ![planned](https://img.shields.io/badge/-planned-lightgrey) |

## Phase 1 at a glance

Ground truth built from one day of AIS (2026-03-03) and 183 Sentinel-2 chips over the Gulf of Oman:

| | |
|---|---|
| AIS fixes / vessels | 52,548 / 182 |
| Optical tiles / ship detections | 183 / 475 |
| Detections corroborated by AIS (≤ 500 m at image time) | 394 (83%) |
| AIS silent at image time | 71 |
| Position discrepancies (spoofing / identity-mismatch candidates) | 10 across 6 vessels |
| Ghost-AIS candidates (AIS in tile, no hull seen) | 24 |

Notes on the data:
- The chips carry no coordinates. They are georeferenced from the Sentinel-2 tile ID and pixel offset, and agree with AIS to within a few pixels.
- AIS timestamps are read as UTC. This was chosen empirically, because UTC minimises optical/AIS disagreement; the `eta` column's `+08` offset is misleading.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows; use .venv/bin/python elsewhere
```

The raw data is not committed. Place it under `applications/maritime_threat_detection/data/raw/` (`raw_ais_data*.xlsx`, `json/`, `png/`), then build the database and run the tests:

```bash
python -m applications.maritime_threat_detection.scripts.ingest_telemetry
python -m pytest
```

The ETL writes `data/processed/maritime.db` and an `ingest_report.json` with the cross-reference statistics above.

## Layout

```text
cat_is_judging/                     domain-agnostic auditing core (phase 2)
applications/maritime_threat_detection/
  db/          schema.sql, AIS spatiotemporal lookups
  tools/       optical annotation parsing (agent tools in phase 3)
  scripts/     ingest_telemetry.py (ETL)
  geo.py       pure-Python WGS84 <-> UTM and Sentinel-2 pixel georeferencing
tests/
```

## License

[MIT](LICENSE)
