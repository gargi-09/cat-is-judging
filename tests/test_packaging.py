"""Release hygiene: what an installed package needs, and where it writes.

The v0.1.0 audit found the wheel shipped without ``schema.sql``: every test passed because
tests run from the source tree. These checks keep that class of bug from coming back; CI
additionally installs the built wheel in a clean environment (``.github/smoke_installed.py``).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

import pytest

from applications.maritime_threat_detection import DATA_RAW, DATASETS_DIR
from applications.maritime_threat_detection.dataset import load_manifest

ROOT = Path(__file__).resolve().parent.parent


def test_every_runtime_data_file_is_packaged() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    missing = []
    for top in ("cat_is_judging", "applications"):
        for f in (ROOT / top).rglob("*"):
            rel = f.relative_to(ROOT)
            if f.is_dir() or f.suffix in (".py", ".pyc", ".md") or "data" in rel.parts or "__pycache__" in rel.parts:
                continue
            ok = any(fnmatch.fnmatch(rel.relative_to(Path(*pkg.split("."))).as_posix(), pat)
                     for pkg, pats in declared.items() if rel.is_relative_to(Path(*pkg.split("."))) for pat in pats)
            if not ok:
                missing.append(rel.as_posix())
    assert not missing, f"not in [tool.setuptools.package-data]: {missing}"


def test_data_directory_can_be_redirected(tmp_path: Path) -> None:
    code = "from applications.maritime_threat_detection import DB_PATH; print(DB_PATH)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True,
                         env=os.environ | {"CAT_IS_JUDGING_DATA": str(tmp_path)}).stdout.strip()
    assert Path(out) == tmp_path.resolve() / "processed" / "maritime.db"


def test_bundled_manifest_follows_the_data_directory() -> None:
    m = load_manifest(DATASETS_DIR / "gulf_of_oman_2026_03_03.json")
    assert m.resolve(m.ais.path) == DATA_RAW / "raw_ais_data.xlsx.xlsx"
    assert m.resolve(m.imagery.annotations) == DATA_RAW / "json"


def test_version_is_exposed() -> None:
    import cat_is_judging

    assert isinstance(cat_is_judging.__version__, str) and cat_is_judging.__version__
