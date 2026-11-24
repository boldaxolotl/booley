"""Doctor reports the Sandbox egress mode in a whole in-Sandbox run (#2132)."""

from __future__ import annotations

import socket

import pytest

from booley.audit.diagnostic_results import Severity
from tests.harness.doctor_run_support import enter_sandbox, run_doctor
from tests.harness.test_doctor import _patch_environment, _write_project


def _resolves(host, *_args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


def _unresolvable(host, *_args):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


@pytest.fixture
def sandbox_doctor(monkeypatch, tmp_path):
    """A Project whose Doctor runs inside the Sandbox, with no ambient egress or proxy."""
    _patch_environment(monkeypatch, tmp_path, _write_project(tmp_path))
    for var in ("BOOLEY_EGRESS", "CLAUDECODE", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    enter_sandbox(monkeypatch)
    return tmp_path


def _rows(root, needle):
    """Run the whole Doctor on *root*; return its rows whose message has *needle*."""
    return [f for f in run_doctor(root).findings if needle in f.message]


@pytest.mark.parametrize(
    ("mode", "resolver", "needle", "severity", "check_id", "fix"),
    [
        ("unproxied", _unresolvable, "Unproxied Sandbox", Severity.WARN, "sandbox.unproxied", ""),
        (None, _unresolvable, "does not resolve", Severity.FAIL, None, "BOOLEY_EGRESS=unproxied"),
        (None, _resolves, "booley-proxy", Severity.PASS, None, ""),
        ("bogus", _resolves, "BOOLEY_EGRESS", Severity.FAIL, None, "`proxy` or `unproxied`"),
    ],
    ids=["unproxied", "unresolved-proxy", "resolved-proxy", "invalid-mode"],
)
def test_egress_mode_row(
    sandbox_doctor, monkeypatch, mode, resolver, needle, severity, check_id, fix
):
    if mode:
        monkeypatch.setenv("BOOLEY_EGRESS", mode)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    [row] = _rows(sandbox_doctor, needle)

    assert (row.severity, row.check_id) == (severity, check_id)
    assert bool(row.fix) == bool(fix)
    assert fix in (row.fix or "")


def test_unproxied_warning_is_waivable(sandbox_doctor, monkeypatch):
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)
    waiver = (
        b'version=1\n[[waiver]]\ncheck="sandbox.unproxied"\nreason="No proxy."\npermanent=true\n'
    )
    (sandbox_doctor / ".booley" / "doctor-waivers.toml").write_bytes(waiver)

    [row] = _rows(sandbox_doctor, "Unproxied Sandbox")

    assert row.severity is Severity.WAIVED


@pytest.mark.parametrize(
    ("delivered", "host"),
    [("http://proxy.corp:3128", "proxy.corp"), ("corp-gw:3128", "corp-gw")],
    ids=["with-scheme", "scheme-less"],
)
def test_delivered_proxy_host_is_the_one_resolved(sandbox_doctor, monkeypatch, delivered, host):
    """A proxy the environment delivered (e.g. `booley shell`) is checked, not booley-proxy."""
    monkeypatch.setenv("HTTPS_PROXY", delivered)
    looked_up = []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda name, *_: looked_up.append(name) or _resolves(name)
    )

    [passed] = _rows(sandbox_doctor, host)

    assert passed.severity is Severity.PASS
    assert host in looked_up
    assert "booley-proxy" not in looked_up
