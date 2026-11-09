"""Read the update description Renovate hands to a post-upgrade command (#2999).

Renovate renders ``dataFileTemplate`` (``.github/renovate.json``) into a file in its
private cache and passes the path only as the environment variable
``RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE``. The repository's post-upgrade scripts
(``pin_checksums.py``, ``renovate_artifacts.py``) share this reader. It is stdlib-only:
the scripts run inside the Renovate container, which has no project environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

DATA_FILE_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"


class Upgrade(NamedTuple):
    """One dependency upgrade of the branch, as the data file template renders it.

    ``dep_name`` is empty for lock-file maintenance, and ``new_value`` for a pin that
    is only a digest.
    """

    dep_name: str
    new_value: str
    package_file: str


def data_file_path(argument: Path | None) -> Path:
    """Return the data file: the command-line argument, else the Renovate environment.

    Raises ``ValueError`` when neither names one.
    """
    if argument is not None:
        return argument
    from_environment = os.environ.get(DATA_FILE_ENV)
    if not from_environment:
        raise ValueError(f"no data file argument and {DATA_FILE_ENV} is not set")
    return Path(from_environment)


def load_upgrades(path: Path) -> list[Upgrade]:
    """Read the upgrades from ``path``; raise ``ValueError`` when the file is unusable."""
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    entries = data.get("upgrades") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no 'upgrades' list (Renovate needs executionMode 'branch')")
    upgrades: list[Upgrade] = []
    for entry in entries:
        dep_name = entry.get("depName") if isinstance(entry, dict) else None
        new_value = entry.get("newValue") if isinstance(entry, dict) else None
        package_file = entry.get("packageFile") if isinstance(entry, dict) else None
        if not (
            isinstance(dep_name, str)
            and isinstance(new_value, str)
            and isinstance(package_file, str)
        ):
            raise ValueError(f"{path}: every upgrade needs a depName, newValue and packageFile")
        upgrades.append(Upgrade(dep_name, new_value, package_file))
    return upgrades
