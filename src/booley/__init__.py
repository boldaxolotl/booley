"""Booley — RTL development harness."""

from pathlib import Path

from booley.runtime.version_attribution import resolve_version_attribution

version_attribution = resolve_version_attribution(Path(__file__))

#: Semantic version belonging to the exact source or distribution imported.
__version__ = version_attribution.version
#: Distribution that supplied ``__version__``; ``None`` for source/fallback.
__dist_name__ = version_attribution.distribution_name
