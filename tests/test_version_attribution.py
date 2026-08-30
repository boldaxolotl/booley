"""Version attribution stays coherent with the imported Booley source."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.runtime.version_attribution import (
    VersionAttribution,
    VersionOrigin,
    resolve_version_attribution,
)

_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _FakeDistribution:
    root: Path
    version: str
    files: tuple[Path, ...] | None = (Path("booley/__init__.py"),)

    def locate_file(self, path: Path) -> Path:
        return self.root / path


def _write_source_tree(root: Path, version: str = "9.9.9\n") -> Path:
    package_file = root / "src" / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "booley-rtl"\n',
        encoding="utf-8",
    )
    (root / "VERSION").write_text(version, encoding="utf-8")
    return package_file


def test_source_tree_version_wins_without_distribution_lookup(tmp_path, monkeypatch) -> None:
    package_file = _write_source_tree(tmp_path)

    def unexpected_distributions(**_kwargs):
        pytest.fail("source attribution must not inspect installed distributions")

    monkeypatch.setattr(importlib.metadata, "distributions", unexpected_distributions)

    assert resolve_version_attribution(package_file) == VersionAttribution(
        version="9.9.9",
        origin=VersionOrigin.SOURCE,
        source_root=tmp_path,
    )


def test_installed_wheel_uses_owning_current_distribution(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    package_file = site_packages / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    owner = _FakeDistribution(site_packages, "4.5.6")

    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda **kwargs: [owner] if kwargs == {"name": "booley-rtl"} else [],
    )

    assert resolve_version_attribution(package_file) == VersionAttribution(
        version="4.5.6",
        origin=VersionOrigin.DISTRIBUTION,
        distribution_name="booley-rtl",
    )


def _write_metadata(
    root: Path,
    distribution: str,
    version: str,
    owned_files: tuple[str, ...] = (),
) -> None:
    normalized = distribution.replace("-", "_")
    dist_info = root / f"{normalized}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    records = "".join(f"{path},,\n" for path in owned_files)
    (dist_info / "RECORD").write_text(
        f"{records}{dist_info.name}/METADATA,,\n{dist_info.name}/RECORD,,\n",
        encoding="utf-8",
    )


def test_imported_checkout_ignores_stale_current_and_legacy_metadata(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    package_file = _write_source_tree(checkout, "9.9.9\n")
    package_file.write_text(
        (_ROOT / "src" / "booley" / "__init__.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runtime_dir = package_file.parent / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    (runtime_dir / "version_attribution.py").write_text(
        (_ROOT / "src" / "booley" / "runtime" / "version_attribution.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    metadata_root = tmp_path / "metadata"
    _write_metadata(metadata_root, "booley-rtl", "1.2.3")
    _write_metadata(metadata_root, "booley", "0.9.0")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(checkout / "src"), str(metadata_root)))

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import booley; print(booley.__version__, booley.__dist_name__)",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9.9.9 None"


def test_harness_reexports_authoritative_top_level_version() -> None:
    import booley
    import booley.harness

    assert booley.harness.__version__ == booley.__version__


def test_real_wheel_metadata_owns_the_imported_package(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    package_file = site_packages / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    _write_metadata(
        site_packages,
        "booley-rtl",
        "4.5.6",
        owned_files=("booley/__init__.py",),
    )
    monkeypatch.syspath_prepend(str(site_packages))

    attribution = resolve_version_attribution(package_file)

    assert attribution.version == "4.5.6"
    assert attribution.origin is VersionOrigin.DISTRIBUTION


def test_distribution_lookup_skips_stale_same_name_metadata(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "active" / "site-packages"
    package_file = site_packages / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    stale = _FakeDistribution(tmp_path / "stale" / "site-packages", "1.2.3")
    owner = _FakeDistribution(site_packages, "4.5.6")

    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda **kwargs: [stale, owner] if kwargs == {"name": "booley-rtl"} else [],
    )

    assert resolve_version_attribution(package_file).version == "4.5.6"


def test_legacy_distribution_is_used_only_when_it_owns_the_import(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    package_file = site_packages / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    unrelated_current = _FakeDistribution(tmp_path / "unrelated", "4.5.6")
    legacy_owner = _FakeDistribution(site_packages, "0.9.0")

    def distributions(**kwargs):
        if kwargs == {"name": "booley-rtl"}:
            return [unrelated_current]
        return [legacy_owner]

    monkeypatch.setattr(importlib.metadata, "distributions", distributions)

    attribution = resolve_version_attribution(package_file)

    assert attribution.version == "0.9.0"
    assert attribution.distribution_name == "booley"


def test_unowned_or_unverifiable_metadata_falls_back_to_dev(tmp_path, monkeypatch) -> None:
    package_file = tmp_path / "vendor" / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    unverifiable = _FakeDistribution(tmp_path, "4.5.6", files=None)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **_kwargs: [unverifiable])

    assert resolve_version_attribution(package_file) == VersionAttribution(
        version="0.0.0-dev",
        origin=VersionOrigin.FALLBACK,
    )


@pytest.mark.parametrize("version_contents", [None, "", "  \n"])
def test_recognized_source_requires_readable_nonempty_version(
    tmp_path, monkeypatch, version_contents
) -> None:
    package_file = _write_source_tree(tmp_path)
    version_file = tmp_path / "VERSION"
    if version_contents is None:
        version_file.unlink()
    else:
        version_file.write_text(version_contents, encoding="utf-8")
    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda **_kwargs: pytest.fail("invalid source must not fall through to metadata"),
    )

    with pytest.raises(RuntimeError, match=str(version_file)):
        resolve_version_attribution(package_file)


def test_recognized_source_reports_unreadable_version(tmp_path) -> None:
    package_file = _write_source_tree(tmp_path)
    version_file = tmp_path / "VERSION"
    version_file.unlink()
    version_file.mkdir()

    with pytest.raises(RuntimeError, match=str(version_file)):
        resolve_version_attribution(package_file)


def test_owning_distribution_requires_nonempty_version(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    package_file = site_packages / "booley" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("", encoding="utf-8")
    owner = _FakeDistribution(site_packages, "")
    monkeypatch.setattr(
        importlib.metadata,
        "distributions",
        lambda **kwargs: [owner] if kwargs == {"name": "booley-rtl"} else [],
    )

    with pytest.raises(RuntimeError, match="owning booley-rtl distribution"):
        resolve_version_attribution(package_file)
