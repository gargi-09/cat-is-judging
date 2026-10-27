"""Maritime Threat & Dark Vessel Detection — FDE reference application for cat-is-judging.

Path constants live here so every script, tool and test resolves data the same way,
regardless of the current working directory.

Where data lives (raw drops and built databases), first match wins:

1. ``CAT_IS_JUDGING_DATA`` environment variable, if set;
2. ``applications/maritime_threat_detection/data`` in a source checkout (the repo layout);
3. ``~/.cat-is-judging/maritime`` for an installed package, so nothing is ever written
   into site-packages.
"""

import os
from pathlib import Path

APP_ROOT: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = APP_ROOT.parent.parent
IN_SOURCE_CHECKOUT: bool = (REPO_ROOT / "pyproject.toml").is_file()


def _data_root() -> Path:
    if env := os.environ.get("CAT_IS_JUDGING_DATA"):
        return Path(env).expanduser().resolve()
    if IN_SOURCE_CHECKOUT:
        return APP_ROOT / "data"
    return Path.home() / ".cat-is-judging" / "maritime"


DATA_ROOT: Path = _data_root()
DATA_RAW: Path = DATA_ROOT / "raw"
DATA_PROCESSED: Path = DATA_ROOT / "processed"
ANNOTATIONS_DIR: Path = DATA_RAW / "json"
IMAGES_DIR: Path = DATA_RAW / "png"
DB_PATH: Path = DATA_PROCESSED / "maritime.db"
DATASETS_DIR: Path = APP_ROOT / "datasets"  # example manifests shipped with the package
