"""Maritime Threat & Dark Vessel Detection — FDE reference application for cat-is-judging.

Path constants live here so every script, tool and test resolves data relative to the
repository root regardless of the current working directory.
"""

from pathlib import Path

APP_ROOT: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = APP_ROOT.parent.parent

DATA_RAW: Path = APP_ROOT / "data" / "raw"
DATA_PROCESSED: Path = APP_ROOT / "data" / "processed"
ANNOTATIONS_DIR: Path = DATA_RAW / "json"
IMAGES_DIR: Path = DATA_RAW / "png"
DB_PATH: Path = DATA_PROCESSED / "maritime.db"
