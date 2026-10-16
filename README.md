# Cat-is-Judging

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Tests](https://img.shields.io/badge/tests-51%20passing-brightgreen?logo=pytest&logoColor=white)](tests/)
[![Status](https://img.shields.io/badge/status-phase%201.5%20of%205-orange)](#roadmap)
[![Interactive Sandbox](https://img.shields.io/badge/interactive-sandbox-7c3aed?logo=githubpages&logoColor=white)](https://gargi-09.github.io/cat-is-judging/)

Neurosymbolic faithfulness auditing for RAG systems: catches hallucinated answers by combining symbolic verification against ground truth with self-consistency scoring.

The library (`cat_is_judging/`) is domain-agnostic. It is showcased through a production-style application, **Maritime Threat & Dark Vessel Detection** (`applications/maritime_threat_detection/`), which reconciles Sentinel-2 satellite imagery against AIS transponder telemetry to find vessels that are dark, spoofing, or phantom.

## Try it in your browser

**[Open the interactive sandbox →](https://gargi-09.github.io/cat-is-judging/)** No install needed.

- **AIS telemetry simulator:** inject a dark vessel, AIS spoofer, ghost signal, misused beacon MMSI, small craft or a transponder going silent, then trigger a satellite pass and watch each hull get classified, both by identity check and by blind (label-free) matching.
- **SDK sandbox:** real SDK code, with an *Execute SDK Sandbox* button that streams the detection log against the live simulation.
- **Feature support matrix:** what ships today and what's next.

The sandbox runs on simulated data, using a JavaScript port of the repo's reconciliation rules. The source is [`docs/index.html`](docs/index.html).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Data engineering: AIS + Sentinel-2 annotations → indexed SQLite ground truth | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 1.5 | Refined threat rules and label-free association, scored against the labels | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 2 | Core SDK: symbolic verifier, consistency scorer, fuser, guardrail interceptor | ![next](https://img.shields.io/badge/-next-blue) |
| 3 | Recon agent and typed AIS / optical tools | ![planned](https://img.shields.io/badge/-planned-lightgrey) |
| 4 | Golden eval set and CI faithfulness gate | ![planned](https://img.shields.io/badge/-planned-lightgrey) |
| 5 | CLI / FastAPI interface and case study | ![planned](https://img.shields.io/badge/-planned-lightgrey) |

## Results so far

One day of AIS (2026-03-03, 52,548 fixes, 182 vessels) against 475 annotated ships in 183 Sentinel-2 chips over the Gulf of Oman. Two modes:

**Identity verification** treats each hull's label as a claim and checks it against AIS:

| Verdict | Count | Threat? |
|---|---|---|
| Corroborated (within 500 m + the estimate's own uncertainty) | 394 | no |
| Position discrepancy (spoofing candidate, 1.4–58 km off) | 10 | **yes** |
| AIS reception gap (insufficient data) | 71 | no |
| Potential going dark (≥ 12 h silence with good fleet reception) | 0 | yes |

**Blind association** ignores the labels and matches hulls to AIS tracks by position, length and heading (Hungarian algorithm). The labels are then used as the answer key:

| Metric | Result |
|---|---|
| Precision | 99.5% (394 right, 2 wrong) |
| Recall, ships AIS can place at image time | 97.5% |
| False dark-vessel alarms | 2 of 475 (0.4%) |
| Unresolved: an AIS gap could explain the hull, queued for review | 77 |

How the rules were set, and what they deliberately don't do:
- **Tolerance:** 500 m is about 2× the observed p90 residual after interpolating AIS to the image second, and each estimate's uncertainty is added on top. A blanket 2–5 km radius would have hidden a real 1.4 km discrepancy.
- **Dead reckoning:** a single fix within 30 min on a moving ship is projected along its speed and course.
- **Dark vessels:** need a hull ≥ 30 m. Sentinel-2 is 10 m resolution, and small craft aren't required to carry AIS.
- **Identity:** MMSIs are checked for ITU structure (beacon, navaid, coast-station prefixes; country code 201–775).
- **Going dark:** follows Global Fishing Watch's definition, a ≥ 12 h gap with good reception. Shorter gaps here are reception gaps: the satellite sees those ships right on their AIS route.
- **Gap routes** are not used to assign identity. A straight line across multi-hour gaps picked the right ship only ~11% of the time in dense anchorages, so it is only used to withhold false dark-vessel alarms.
- **Dataset limit:** the labels were themselves made by matching to AIS, so the data contains no true dark vessels. Dark and spoofing behaviour is demonstrated with injected cases, and every alert is a *candidate* for review.
- **Georeferencing:** the chips carry no coordinates. They are georeferenced from the Sentinel-2 tile ID and pixel offset, and agree with AIS to within a few pixels.
- **Timestamps:** AIS timestamps are read as UTC. This was chosen empirically; the `eta` column's `+08` offset is misleading.

## Quickstart

The fastest way to see it work is the **[interactive sandbox](https://gargi-09.github.io/cat-is-judging/)**. Nothing to install.

To run the code locally, install it and run the test suite. The tests use small synthetic fixtures, so no dataset is needed:

```bash
git clone https://github.com/gargi-09/cat-is-judging.git
cd cat-is-judging
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows; use .venv/bin/python elsewhere
python -m pytest
```

> **Have the raw data?** The AIS and Sentinel-2 files aren't distributed with the repo. If you have them, place them under `applications/maritime_threat_detection/data/raw/` (`raw_ais_data*.xlsx`, `json/`, `png/`). Then `python -m applications.maritime_threat_detection.scripts.ingest_telemetry` builds the database and an `ingest_report.json` with the results above.


## Layout

```text
cat_is_judging/                     domain-agnostic auditing core (phase 2)
applications/maritime_threat_detection/
  db/          schema.sql, AIS spatiotemporal lookups
  tools/       optical annotation parsing (agent tools in phase 3)
  scripts/     ingest_telemetry.py (ETL)
  reconciliation.py  threat rules, identity verification, blind association
  geo.py       pure-Python WGS84 <-> UTM and Sentinel-2 pixel georeferencing
tests/
```

## License

[MIT](LICENSE)
