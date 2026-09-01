"""Shared PDK path conventions for synthesis backends."""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_LIB_DIR = Path("C:/tools") if sys.platform == "win32" else Path("/opt/pdk")
