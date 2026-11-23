"""Desktop library and document names, including read access to existing installations."""
import os
from pathlib import Path

PROJECT_SUFFIX = ".beamweft"
PACKAGE_FORMAT = "beamweft-case"
DATABASE_NAME = "beamweft.db"

# Only opening previously written data needs the old on-disk names.
PREVIOUS_PROJECT_SUFFIX = ".lclaw"
PREVIOUS_FORMAT = "laserclaw-case"
PREVIOUS_DATABASE = "laserclaw.db"
PREVIOUS_DATA_ROOT = "LaserClaw-Data"
PREVIOUS_DATA_ENV = "LASERCLAW_DATA_DIR"


def database_path(directory):
    directory = Path(directory)
    current, previous = directory / DATABASE_NAME, directory / PREVIOUS_DATABASE
    return previous if not current.exists() and previous.is_file() else current


def default_library_root():
    home = Path.home()
    current, previous = home / "BeamWeft-Data", home / PREVIOUS_DATA_ROOT
    # A preview can create the new desktop-ui cache before the user opens their
    # library. A cache alone is not evidence that they established a new library.
    if not database_path(current).is_file() and previous.is_dir():
        if database_path(previous).is_file() or any((previous / name).is_dir() for name in ("opened-cases", "uploads")):
            return previous.resolve()
    return current.resolve()


def library_data_root():
    selected = os.environ.get("BEAMWEFT_DATA_DIR") or os.environ.get(PREVIOUS_DATA_ENV)
    return Path(selected).resolve() if selected else default_library_root()
