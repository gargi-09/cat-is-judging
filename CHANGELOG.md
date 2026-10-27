# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/); before 1.0 the API may change between minor versions.

## [0.1.0] - 2026-10-26

First tagged release. The auditor core is stable enough to try; the maritime application is its reference domain.

### Auditor core (`cat_is_judging`)
- Atomic claims and reports.
- Symbolic verifier: rules give each claim a three-valued verdict (supported / contradicted / unverifiable) and fail closed: a crashing or missing rule never lets a claim through.
- Self-consistency scorer over independent re-generations.
- Fusion: symbolic verdicts outrank consistency; unverifiable claims are capped at 0.5, so a report passes 0.95 only with at most 10% unverified claims.
- Guardrail: pass, amend (cut contradicted claims, mark unverifiable ones `[unverified]`) or block (contradicted critical claim, score below 0.5, or nothing verified left).

### Maritime application
- Ground truth: AIS + Sentinel-2 annotations into an indexed SQLite store, rebuilt from raw files and swapped in atomically, with lineage; read-only downstream.
- Track estimation with per-estimate uncertainty (interpolation, stationary hold, dead reckoning, or an explicit "can't place it").
- Identity verification and label-free blind association. On one day over the Gulf of Oman: 99.5% precision, 97.5% recall on AIS-placeable ships, 0.4% false dark-vessel alarms.
- Dataset adapter: `dataset.json` manifests with column mapping and label provenance, any Sentinel-2 tile (metadata, registry or MGRS-derived origin), georeferenced-CSV imagery, and a label-free AIS clock sweep that reports "ambiguous" rather than guess.
- Audit rules for five claim kinds; presence decided by imagery, identity by AIS.
- LLM recon analyst (Claude Opus 5.5) with seven typed, read-only tools and a structured report.

### Packaging
- Installable wheel with its data files (`schema.sql`, example manifests); a packaging test and a clean-install CI job guard against missing files.
- Data directory outside the package: `CAT_IS_JUDGING_DATA`, the repo's `data/` folder in a clone, or `~/.cat-is-judging/maritime`.
- CI on Python 3.10–3.13.

### Known limitations
- The LLM analyst is tested offline only (scripted client); no live results are published yet.
- Results come from one day, one region, one satellite pass. The labels were made by matching to AIS, so the data contains no true dark vessels; dark-vessel and spoofing behaviour is shown with injected cases. Every alert is a candidate for review.
- The maritime package is importable as `applications.maritime_threat_detection`; it will move to a namespaced package before 1.0.
- Not yet on PyPI; install from the Git tag.

[0.1.0]: https://github.com/gargi-09/cat-is-judging/releases/tag/v0.1.0
