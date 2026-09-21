from pathlib import Path

from booley.runtime.host_install import host_install_error


def test_accepts_base_interpreter_installed_wheel() -> None:
    source = Path("/opt/python/lib/python3.14/site-packages/booley/data/skills")
    assert host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr")) is None


def test_rejects_virtual_environment_wheel() -> None:
    source = Path("/project/.venv/lib/python3.14/site-packages/booley/data/skills")
    error = host_install_error(source, prefix=Path("/project/.venv"), base_prefix=Path("/usr"))
    assert error is not None
    assert "virtual environment" in error


def test_rejects_source_checkout() -> None:
    source = Path("/work/Booley/src/booley/data/skills")
    error = host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr"))
    assert error is not None
    assert "not from an installed wheel" in error


def test_rejects_qa_runtime_even_when_it_contains_site_packages() -> None:
    source = Path(
        "/work/Booley/.runtime/qa-runs/run/operator-venv/"
        "lib/python3.14/site-packages/booley/data/skills"
    )
    error = host_install_error(source, prefix=Path("/usr"), base_prefix=Path("/usr"))
    assert error is not None
    assert "ephemeral workspace state" in error
