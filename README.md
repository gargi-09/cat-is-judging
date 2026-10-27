# Cat-is-Judging

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Pydantic v2](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![CI](https://github.com/gargi-09/cat-is-judging/actions/workflows/ci.yml/badge.svg)](https://github.com/gargi-09/cat-is-judging/actions/workflows/ci.yml)
[![Release](https://img.shields.io/badge/release-v0.1.0-blue)](CHANGELOG.md)
[![Status](https://img.shields.io/badge/status-phase%203%20of%205-orange)](#roadmap)
[![Interactive Sandbox](https://img.shields.io/badge/interactive-sandbox-7c3aed?logo=githubpages&logoColor=white)](https://gargi-09.github.io/cat-is-judging/)

Neurosymbolic faithfulness auditing for RAG systems: catches hallucinated answers by combining symbolic verification against ground truth with self-consistency scoring.

The library (`cat_is_judging/`) is domain-agnostic. It is showcased through a production-style application, **Maritime Threat & Dark Vessel Detection** (`applications/maritime_threat_detection/`), which reconciles Sentinel-2 satellite imagery against AIS transponder telemetry to find vessels that are dark, spoofing, or phantom.

## Try it in your browser

**[Open the interactive sandbox →](https://gargi-09.github.io/cat-is-judging/)** No install needed.

- **AIS telemetry simulator:** inject a dark vessel, AIS spoofer, ghost signal, misused beacon MMSI, small craft or a transponder going silent, then trigger a satellite pass and watch each hull get classified, both by identity check and by blind (label-free) matching.
- **SDK sandbox:** real SDK code, with an *Execute SDK Sandbox* button that streams the detection log against the live simulation, including the faithfulness audit and guardrail.
- **Feature support matrix:** what ships today and what's next.

The sandbox runs on simulated data, using a JavaScript port of the repo's reconciliation rules and auditor core. The source is [`docs/index.html`](docs/index.html).

## Roadmap

**Latest release: v0.1.0** (Phases 1–3). See the [changelog](CHANGELOG.md) for what's in it and its known limitations. v0.2 adds the Phase 4 evaluation set and CI faithfulness gate; 1.0 follows Phase 5.

| Phase | Scope | Status |
|---|---|---|
| 1 | Data engineering: AIS + Sentinel-2 annotations → indexed SQLite ground truth | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 1.5 | Refined threat rules and label-free association, scored against the labels | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 2 | Core SDK: symbolic verifier, consistency scorer, fuser, guardrail interceptor | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 2.5 | Dataset adapter: manifests, any Sentinel-2 tile, georeferenced imagery, label-free clock sweep | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 3 | Recon agent and typed AIS / optical tools | ![done](https://img.shields.io/badge/-done-brightgreen) |
| 4 | Golden eval set and CI faithfulness gate | ![next](https://img.shields.io/badge/-next-blue) |
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

Three of the six vessels behind the spoofing candidates sat at anchor on their hulls until about 05:20. Then their AIS jumped tens of km away, reporting 33–48 knots, while the satellite still saw them at the anchorage at 06:47. That is consistent with spoofing or GNSS interference, and they stay *candidates* for review.

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
- **Timestamps:** AIS timestamps are read as UTC. This was chosen empirically; the `eta` column's `+08` offset is misleading. The label-free clock sweep (below) says "ambiguous" on this data rather than guess: most ships are anchored, which says nothing about the clock.

## The auditor (Phase 2)

A report is split into atomic claims. Each claim gets a **three-valued verdict** from deterministic rules: *supported*, *contradicted* or *unverifiable* (the rule can't decide, so it doesn't guess). Separately, the generator is **re-sampled**, and each claim's consistency is the share of samples that repeat it. The two are fused into one score per claim:

| Symbolic verdict | Claim score | Why |
|---|---|---|
| contradicted | 0 | A hallucination repeated in every sample is still a hallucination |
| supported | 1 | A verified fact is faithful even if the generator was unsure of it |
| unverifiable | consistency × 0.5 | Consistency is evidence of belief, not truth, so it is capped |

The report score is the mean over claims. With a 0.95 pass bar, the 0.5 cap means a report passes only if at most 10% of its claims are unverified. The **guardrail** then decides:
- **pass:** ≥ 0.95 and nothing contradicted.
- **amend:** contradicted claims are cut, unverifiable ones are marked `[unverified]`. Hedging excuses missing evidence, never contradicting evidence.
- **block:** a critical claim (an accusation such as spoofing or going dark) is contradicted, the score is below 0.5, or nothing verified would remain.

The core (`cat_is_judging/`) knows nothing about ships. The maritime plug-in supplies five claim kinds, with **presence decided by the imagery** and **identity by AIS**. Identity is often only unverifiable: "not in my feed" isn't "unregistered", and a short AIS gap isn't going dark.

On the real data, three stand-in analysts each wrote one report per tile (183 tiles, 5 samples each):

| Analyst | Pass | Amend | Block |
|---|---|---|---|
| `imagery_first`: reports what the satellite saw, label-free identities | 137 | 46 | 0 |
| `ais_trusting`: repeats AIS, including positions with no hull behind them | 115 | 54 | 14 |
| `alarmist`: calls every AIS lapse "going dark" | 115 | 33 | 35 |

The `imagery_first` amendments are tiles where AIS gaps leave "no dark vessels" unprovable, so that claim is hedged rather than asserted. Two `ais_trusting` blocks trace back to blind association's own 2 false dark alarms: the auditor is only as good as its rules. These rule-based analysts stay as cheap, reproducible baselines; the LLM analyst below is the real subject.

## The analyst (Phase 3)

The thing being audited is an LLM analyst: Claude Opus 5.5 investigates one tile through seven typed, read-only tools and writes a structured report. The report is split into atomic claims, and the auditor and guardrail above decide what is released.

- **Same evidence a human analyst would get.** The tools return hulls with position, length and orientation but **no identity** (the annotator labels were made from AIS, so showing them would be circular), plus AIS estimates with their uncertainty, raw tracks, MMSI validity, and the 12 h going-dark assessment.
- **Measurements, not verdicts.** `compare_hull_to_ais` returns distance and tolerance, never "spoofing". Calling something a spoofer is the analyst's claim, and the auditor checks it.
- **Report as structured output.** The final report is schema-constrained JSON (`output_config.format`), not a forced tool call, which Opus 5.5 doesn't allow.
- **Consistency samples are re-runs.** Thinking is always on and `temperature` isn't available on this model, so self-consistency compares independent runs. Refusals fall back server-side (`fallbacks: "default"`).
- **Status:** tested offline with a scripted client: request shape, tool round trips, errors, refusals, the turn budget, and a hallucinated ghost being cut even when the analyst repeats it in every sample. **It has not yet been run against the live API, so there are no LLM results to report.** Run it from the local dashboard or in code (needs `ANTHROPIC_API_KEY`):

```python
from applications.maritime_threat_detection.analyst import ReconAnalyst
from applications.maritime_threat_detection.audit import MaritimeGroundTruth, make_guardrail

decision = make_guardrail().intercept(ReconAnalyst().generator(gt, tile_id), gt, n_samples=2)
```

## Bring your own dataset

A new AIS + imagery dataset is described in a `dataset.json` manifest, not in code. The manifest says where the files are, which source column means what, what timezone the AIS clock is in (or `"auto"`), and how the labels were made. See [`datasets/example_georeferenced.json`](applications/maritime_threat_detection/datasets/example_georeferenced.json).

```bash
python -m applications.maritime_threat_detection.scripts.ingest_telemetry --manifest path/to/dataset.json
```

- **Imagery:** Sentinel-2 Labelme chips from any tile, in either hemisphere, or detections already in lat/lon (for example from SAR) plus a file of scene footprints. A tile's origin comes from its `MTD_TL.xml` when supplied (exact), else the MGRS square (within ~45 m).
- **Columns:** only `mmsi`, time, `lat` and `lon` are required from AIS. A mistyped column name is an error, not a silent gap. Unusable rows (no hull length, unknown scene, outside the footprint) are dropped with a reason, never guessed.
- **Label provenance:** `ais_matched`, `independent` or `none`. **Dark-vessel recall** is reported only for `independent` labels; with AIS-matched labels it would be circular, and the report says so.
- **Clock:** `"utc_offset": "auto"` runs a label-free sweep over every whole-hour timezone. Only moving ships count, because an anchored ship is in the same place at any offset. A naive version that counted every matched hull picked UTC−2 on this data. It was really measuring AIS coverage, inflated by the ships whose AIS jumped away shortly before the image. The sweep's answer must be two standard errors clear of the runner-up, or it reports "ambiguous" and keeps UTC, noted in the lineage record.
- Each dataset builds its own read-only store (`data/processed/<name>.db`), with the manifest and timezone decision recorded in `ingest_runs`.

## Quickstart

The fastest way to see it work is the **[interactive sandbox](https://gargi-09.github.io/cat-is-judging/)**. Nothing to install.

**Install the release** (Python 3.10+). The core auditor needs only Pydantic; add `[maritime]` for the maritime application and `[agent]` for the LLM analyst:

```bash
pip install "cat-is-judging[maritime] @ git+https://github.com/gargi-09/cat-is-judging@v0.1.0"
```

```python
from cat_is_judging import Claim, FaithfulnessAuditor, Guardrail, Report, rule

@rule("capital")
def capital(claim, truth):
    if truth.get(claim.subject) is None:
        return "unverifiable", "no record"
    ok = truth[claim.subject] == claim.attributes["city"]
    return ("supported" if ok else "contradicted"), f"record says {truth[claim.subject]}"

report = Report(id="r1", claims=(
    Claim(id="a", kind="capital", subject="France", attributes={"city": "Paris"}, text="France's capital is Paris"),
    Claim(id="b", kind="capital", subject="Japan", attributes={"city": "Kyoto"}, text="Japan's capital is Kyoto"),
))
decision = Guardrail(FaithfulnessAuditor([capital])).review(report, {"France": "Paris", "Japan": "Tokyo"})
print(decision.action)              # amend
print(decision.released.render())   # - France's capital is Paris   (the false claim is cut)
```

**Develop or run the tests** from a clone. The tests use small synthetic fixtures, so no dataset is needed:

```bash
git clone https://github.com/gargi-09/cat-is-judging.git
cd cat-is-judging
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[maritime,agent,dev]"   # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m pytest
```

`requirements.txt` pins the exact environment the published results were produced with (Python 3.11+).

> **Have the raw data?** The AIS and Sentinel-2 files aren't distributed with the repo. Data lives in `applications/maritime_threat_detection/data/` in a clone, `~/.cat-is-judging/maritime/` for an installed package, or wherever `CAT_IS_JUDGING_DATA` points. Put the files under its `raw/` folder (`raw_ais_data*.xlsx`, `json/`, `png/`), then `python -m applications.maritime_threat_detection.scripts.ingest_telemetry` builds the database and an `ingest_report.json` with the results above.


## Layout

```text
cat_is_judging/                     domain-agnostic auditing core
  claims.py    atomic claims and reports
  evaluators/  symbolic verifier, self-consistency, fusion
  guardrails/  pass / amend / block
  auditor.py   FaithfulnessAuditor
applications/maritime_threat_detection/
  audit.py     maritime claim kinds and rules for the auditor
  analyst.py   LLM recon analyst (Claude Opus 5.5, tool loop, structured report)
  analyst_stub.py  rule-based baseline analysts
  dataset.py   manifests, column mapping, imagery loaders
  clock_offset.py  label-free AIS timezone sweep
  datasets/    example manifests
  db/          schema.sql, AIS spatiotemporal lookups
  tools/       optical annotation parsing; agent_tools.py: the analyst's read-only tools
  scripts/     ingest_telemetry.py (ETL)
  reconciliation.py  threat rules, identity verification, blind association
  geo.py       pure-Python WGS84 <-> UTM and Sentinel-2 pixel georeferencing
tests/
```

## License

[MIT](LICENSE)
