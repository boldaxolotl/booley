"""Contracts for the runtime-base package inventory helper (#829)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_HELPER = Path(__file__).parents[2] / "src/booley/data/docker/base_package_inventory.py"
_SPEC = importlib.util.spec_from_file_location("base_package_inventory", _HELPER)
assert _SPEC is not None and _SPEC.loader is not None
inventory = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inventory)

Package = inventory.Package


def _row(name: str, version: str, arch: str = "amd64", status: str = "installed") -> str:
    return f"{name}\t{version}\t{arch}\t{status}\tok"


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": 1,
        "scope": "runtime-base",
        "python": {"version": "3.13.15", "pip_version": "26.2.1"},
        "packages": [
            {"name": "libc6", "version": "2.39-0ubuntu8.6", "architecture": "amd64"},
            {"name": "zlib1g", "version": "1:1.3.dfsg-3.1ubuntu2.1", "architecture": "amd64"},
        ],
    }
    document.update(overrides)
    return document


def _completed(stdout: bytes, returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b"boom")


# --- dpkg collection -------------------------------------------------------


def test_installed_rows_are_kept_and_absent_rows_are_dropped() -> None:
    output = "\n".join(
        [
            _row("gcc", "4:13.2.0-7ubuntu1"),
            _row("removed", "1.0", status="not-installed"),
            _row("purged-later", "2.0", status="config-files"),
            "",
        ]
    )

    assert inventory.parse_dpkg_rows(output) == [Package("gcc", "4:13.2.0-7ubuntu1", "amd64")]


def test_held_installed_package_is_included() -> None:
    # Hold is a desired action; db:Status-Status stays "installed".
    assert inventory.parse_dpkg_rows(_row("held-pkg", "1.2-3")) == [
        Package("held-pkg", "1.2-3", "amd64")
    ]


@pytest.mark.parametrize(
    "status",
    ["unpacked", "half-installed", "half-configured", "triggers-awaited", "triggers-pending"],
)
def test_intermediate_states_are_rejected_not_dropped(status: str) -> None:
    with pytest.raises(inventory.InventoryError, match=status):
        inventory.parse_dpkg_rows(_row("partial", "1.0", status=status))


def test_reinstall_required_error_flag_is_rejected() -> None:
    with pytest.raises(inventory.InventoryError, match="reinstreq"):
        inventory.parse_dpkg_rows("broken\t1.0\tamd64\tinstalled\treinstreq")


@pytest.mark.parametrize("line", ["a\t1\tamd64\tinstalled", "a\t1\tamd64\tinstalled\tok\textra"])
def test_wrong_field_count_is_rejected(line: str) -> None:
    with pytest.raises(inventory.InventoryError, match="fields"):
        inventory.parse_dpkg_rows(line)


def test_multiarch_identities_remain_separate_rows() -> None:
    packages = inventory.parse_dpkg_rows(
        "\n".join([_row("libc6", "2.39", "i386"), _row("libc6", "2.39", "amd64")])
    )
    document = inventory.build_inventory(packages, "3.13.15", "26.2.1")

    assert [(row["name"], row["architecture"]) for row in document["packages"]] == [
        ("libc6", "amd64"),
        ("libc6", "i386"),
    ]


def test_duplicate_identity_is_rejected() -> None:
    packages = [Package("libc6", "2.39", "amd64"), Package("libc6", "2.40", "amd64")]

    with pytest.raises(inventory.InventoryError, match="duplicate"):
        inventory.build_inventory(packages, "3.13.15", "26.2.1")


def test_collect_dpkg_uses_c_locale_explicit_fields_and_a_bound() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return _completed(_row("bash", "5.2.21-2ubuntu4").encode())

    assert inventory.collect_dpkg(run) == [Package("bash", "5.2.21-2ubuntu4", "amd64")]
    argv, kwargs = calls[0]
    assert argv[:3] == ["dpkg-query", "-W", "-f"]
    for field in ("Package", "Version", "Architecture", "db:Status-Status", "db:Status-Eflag"):
        assert f"${{{field}}}" in argv[3]
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["timeout"] == inventory.COMMAND_TIMEOUT_SECONDS


def test_failed_dpkg_query_is_an_error() -> None:
    with pytest.raises(inventory.InventoryError, match="exited 2"):
        inventory.collect_dpkg(lambda *_args, **_kwargs: _completed(b"", returncode=2))


def test_timed_out_dpkg_query_is_an_error() -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("dpkg-query", 60)

    with pytest.raises(inventory.InventoryError, match="dpkg-query failed"):
        inventory.collect_dpkg(run)


def test_oversized_dpkg_output_is_rejected_not_truncated() -> None:
    huge = b"x" * (inventory.MAX_INVENTORY_BYTES + 1)

    with pytest.raises(inventory.InventoryError, match="exceeds"):
        inventory.collect_dpkg(lambda *_args, **_kwargs: _completed(huge))


# --- pip and interpreter versions -------------------------------------------


def test_imported_and_metadata_pip_versions_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata
    import types

    monkeypatch.setitem(sys.modules, "pip", types.SimpleNamespace(__version__="1.0"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "2.0")

    with pytest.raises(inventory.InventoryError, match="disagrees"):
        inventory.python_versions()


def test_absent_pip_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pip", None)

    with pytest.raises(inventory.InventoryError, match="pip is not importable"):
        inventory.python_versions()


def test_isolated_collection_ignores_python_path_overrides(tmp_path: Path) -> None:
    shadow = tmp_path / "pip"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("__version__ = '999.shadow'\n", encoding="utf-8")
    probe = (
        f"import importlib.util, sys; spec = importlib.util.spec_from_file_location('h', {str(_HELPER)!r}); "
        "h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)\n"
        "try:\n    print(h.python_versions())\nexcept h.InventoryError as e:\n    print(e)"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(tmp_path), "PYTHONUSERBASE": str(tmp_path)},
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "999.shadow" not in result.stdout


def test_collection_commands_refuse_non_isolated_mode(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(_HELPER), "generate", "--output", str(tmp_path / "out.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 1
    assert "python -I" in result.stderr
    assert not (tmp_path / "out.json").exists()


# --- serialization determinism ----------------------------------------------


def test_reordered_inputs_serialize_to_identical_bytes() -> None:
    rows = [Package("zlib1g", "1:1.3", "amd64"), Package("bash", "5.2", "amd64")]
    first = inventory.serialize(inventory.build_inventory(rows, "3.13.15", "26.2.1"))
    second = inventory.serialize(inventory.build_inventory(rows[::-1], "3.13.15", "26.2.1"))

    assert first == second
    assert first.endswith(b"}\n") and not first.endswith(b"\n\n")
    assert json.loads(first)["packages"][0]["name"] == "bash"


@pytest.mark.parametrize(
    ("rows", "python_version", "pip_version"),
    [
        ([Package("bash", "5.3", "amd64")], "3.13.15", "26.2.1"),
        ([Package("bash", "5.2", "amd64")], "3.13.16", "26.2.1"),
        ([Package("bash", "5.2", "amd64")], "3.13.15", "26.3"),
    ],
)
def test_version_changes_change_bytes(
    rows: list[Any], python_version: str, pip_version: str
) -> None:
    baseline = inventory.serialize(
        inventory.build_inventory([Package("bash", "5.2", "amd64")], "3.13.15", "26.2.1")
    )

    changed = inventory.serialize(inventory.build_inventory(rows, python_version, pip_version))

    assert changed != baseline


def test_serialization_has_no_volatile_fields() -> None:
    document = inventory.build_inventory([Package("bash", "5.2", "amd64")], "3.13.15", "26.2.1")

    assert set(json.loads(inventory.serialize(document))) == {
        "schema",
        "scope",
        "python",
        "packages",
    }


# --- validation ---------------------------------------------------------------


def test_valid_document_round_trips_debian_and_pip_versions() -> None:
    document = _document(python={"version": "3.13.15", "pip_version": "26.2.1.post1"})
    document["packages"][0]["version"] = "1:2.39~rc1+dfsg-0ubuntu8.6"

    assert inventory.parse_inventory(inventory.serialize(document)) == document


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.update(schema=True), "schema"),
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d.update(scope="sandbox"), "scope"),
        (lambda d: d.pop("python"), "missing"),
        (lambda d: d.update(extra=1), "unknown"),
        (lambda d: d.update(packages=[]), "nonempty list"),
        (lambda d: d["python"].update(version="3.14.0rc1"), "final"),
        (lambda d: d["python"].update(version=3.13), "nonempty string"),
        (lambda d: d["python"].update(pip_version=""), "nonempty string"),
        (lambda d: d["packages"][0].update(version=""), "nonempty string"),
        (lambda d: d["packages"][0].pop("architecture"), "missing"),
        (lambda d: d["packages"].reverse(), "not sorted"),
        (lambda d: d["packages"].append(dict(d["packages"][1])), "duplicate"),
        (lambda d: d.update(packages=["libc6"]), "expected an object"),
    ],
)
def test_validator_reports_contract_violations(mutation: Any, message: str) -> None:
    document = _document()
    mutation(document)

    errors = inventory.validate_inventory(document)

    assert any(message in error for error in errors), errors


@pytest.mark.parametrize("data", [b"", b"{", b'{"schema": 1', b"\xff\xfe"])
def test_invalid_or_truncated_json_is_rejected(data: bytes) -> None:
    with pytest.raises(inventory.InventoryError, match="not UTF-8 JSON"):
        inventory.parse_inventory(data)


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    path.write_bytes(b" " * (inventory.MAX_INVENTORY_BYTES + 1))

    with pytest.raises(inventory.InventoryError, match="exceeds"):
        inventory.read_inventory_file(path)


@pytest.mark.parametrize(
    ("contents", "returncode"),
    [(None, 0), (b"{}", 1), (None, 1)],
    ids=["valid", "malformed", "missing"],
)
def test_validate_cli_exit_status(tmp_path: Path, contents: bytes | None, returncode: int) -> None:
    path = tmp_path / "inventory.json"
    if contents is None and returncode == 0:
        path.write_bytes(inventory.serialize(_document()))
    elif contents is not None:
        path.write_bytes(contents)

    result = subprocess.run(
        [sys.executable, str(_HELPER), "validate", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == returncode, result.stderr


# --- current-state verification and atomic output ---------------------------


def test_verify_current_reports_package_and_python_drift() -> None:
    stored = _document()
    current = _document(python={"version": "3.13.16", "pip_version": "26.2.1"})
    current["packages"] = [
        {"name": "libc6", "version": "2.39-0ubuntu8.7", "architecture": "amd64"},
        {"name": "newpkg", "version": "1", "architecture": "amd64"},
    ]

    errors = inventory.verify_current(stored, current)

    assert any(error.startswith("python:") for error in errors)
    assert any("libc6:amd64" in error for error in errors)
    assert any("newpkg:amd64: stored <absent>" in error for error in errors)
    assert any("zlib1g:amd64" in error and "current <absent>" in error for error in errors)
    assert inventory.verify_current(stored, _document()) == []


def test_atomic_write_publishes_complete_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    data = inventory.serialize(_document())

    inventory.write_atomically(path, data)

    assert path.read_bytes() == data
    assert sorted(child.name for child in tmp_path.iterdir()) == ["inventory.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permission bits require POSIX")
def test_atomic_write_sets_readable_posix_permissions(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"

    inventory.write_atomically(path, inventory.serialize(_document()))

    assert path.stat().st_mode & 0o777 == 0o644
