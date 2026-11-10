"""Version the site's stylesheet URLs by content, so a deploy reaches every visitor.

GitHub Pages serves files with ``Cache-Control: max-age=600``. With fixed
stylesheet URLs, a returning visitor can get a freshly deployed page with the
previous stylesheet for up to ten minutes, and markup that needs the new rules
(such as an SVG figure, which paints black without them) breaks. This hook
gives base.html a short content hash per ``extra_css`` file; the template
appends it as ``?v=<hash>``, so changed CSS gets a new URL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

HASH_LENGTH = 12


def asset_versions(docs_dir: Path, paths: list[str]) -> dict[str, str]:
    """Return a short SHA-256 of each file in ``paths`` (relative to ``docs_dir``)."""
    return {
        path: hashlib.sha256((docs_dir / path).read_bytes()).hexdigest()[:HASH_LENGTH]
        for path in paths
    }


def on_env(env, config, files):
    """Expose ``asset_versions`` to the templates."""
    env.globals["asset_versions"] = asset_versions(
        Path(config["docs_dir"]), [str(path) for path in config["extra_css"]]
    )
    return env
