"""Tests for booley.docker.proxy_entry — containerized egress proxy entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "src" / "booley" / "docker")
)


class TestMainEnvParsing:
    """Test environment variable parsing in main()."""

    @patch("proxy_entry.EgressProxy")
    @patch("proxy_entry.signal.signal")
    def test_default_port(self, mock_signal, mock_proxy_cls, monkeypatch):
        monkeypatch.delenv("PROXY_PORT", raising=False)
        monkeypatch.delenv("PROXY_ALLOWLIST", raising=False)
        mock_proxy = MagicMock()
        mock_proxy.port = 8080
        mock_proxy.stats = MagicMock(allowed=0, blocked=0, errors=0, blocked_log=[])
        mock_proxy_cls.return_value = mock_proxy

        stop_event = MagicMock()
        stop_event.wait = MagicMock(return_value=True)
        with patch("proxy_entry.threading.Event", return_value=stop_event):
            import proxy_entry

            proxy_entry.main()

        mock_proxy_cls.assert_called_once_with(port=8080, host="0.0.0.0", allowlist=None)

    @patch("proxy_entry.EgressProxy")
    @patch("proxy_entry.signal.signal")
    def test_custom_port(self, mock_signal, mock_proxy_cls, monkeypatch):
        monkeypatch.setenv("PROXY_PORT", "9090")
        monkeypatch.delenv("PROXY_ALLOWLIST", raising=False)
        mock_proxy = MagicMock()
        mock_proxy.port = 9090
        mock_proxy.stats = MagicMock(allowed=0, blocked=0, errors=0, blocked_log=[])
        mock_proxy_cls.return_value = mock_proxy

        stop_event = MagicMock()
        stop_event.wait = MagicMock(return_value=True)
        with patch("proxy_entry.threading.Event", return_value=stop_event):
            import proxy_entry

            proxy_entry.main()

        mock_proxy_cls.assert_called_once_with(port=9090, host="0.0.0.0", allowlist=None)

    def test_invalid_port_exits(self, monkeypatch):
        monkeypatch.setenv("PROXY_PORT", "notanumber")
        with pytest.raises(SystemExit):
            import proxy_entry

            proxy_entry.main()

    def test_invalid_allowlist_json_exits(self, monkeypatch):
        monkeypatch.setenv("PROXY_PORT", "8080")
        monkeypatch.setenv("PROXY_ALLOWLIST", "not json")
        with pytest.raises(SystemExit):
            import proxy_entry

            proxy_entry.main()

    def test_allowlist_not_array_exits(self, monkeypatch):
        monkeypatch.setenv("PROXY_PORT", "8080")
        monkeypatch.setenv("PROXY_ALLOWLIST", '{"not": "array"}')
        with pytest.raises(SystemExit):
            import proxy_entry

            proxy_entry.main()

    def test_allowlist_non_string_element_exits(self, monkeypatch):
        # A non-string element would reach allowed.lower() in
        # EgressProxy._is_allowed and raise AttributeError mid-request.
        monkeypatch.setenv("PROXY_PORT", "8080")
        monkeypatch.setenv("PROXY_ALLOWLIST", '["ok.com", 123]')
        with pytest.raises(SystemExit):
            import proxy_entry

            proxy_entry.main()

    @patch("proxy_entry.EgressProxy")
    @patch("proxy_entry.signal.signal")
    def test_valid_allowlist(self, mock_signal, mock_proxy_cls, monkeypatch):
        monkeypatch.setenv("PROXY_PORT", "8080")
        monkeypatch.setenv("PROXY_ALLOWLIST", '["example.com", "api.test.io"]')
        mock_proxy = MagicMock()
        mock_proxy.port = 8080
        mock_proxy.stats = MagicMock(allowed=1, blocked=2, errors=0, blocked_log=["x"])
        mock_proxy_cls.return_value = mock_proxy

        stop_event = MagicMock()
        stop_event.wait = MagicMock(return_value=True)
        with (
            patch("proxy_entry.threading.Event", return_value=stop_event),
            patch("builtins.print"),
        ):
            import proxy_entry

            proxy_entry.main()

        mock_proxy_cls.assert_called_once_with(
            port=8080,
            host="0.0.0.0",
            allowlist=["example.com", "api.test.io"],
        )


class TestStatsOutput:
    @patch("proxy_entry.EgressProxy")
    @patch("proxy_entry.signal.signal")
    def test_prints_stats_json_on_shutdown(self, mock_signal, mock_proxy_cls, monkeypatch, capsys):
        monkeypatch.delenv("PROXY_PORT", raising=False)
        monkeypatch.delenv("PROXY_ALLOWLIST", raising=False)
        mock_proxy = MagicMock()
        mock_proxy.port = 8080
        mock_proxy.stats = MagicMock(
            allowed=5,
            blocked=3,
            errors=1,
            blocked_log=["CONNECT evil.com:443"],
        )
        mock_proxy_cls.return_value = mock_proxy

        stop_event = MagicMock()
        stop_event.wait = MagicMock(return_value=True)
        with patch("proxy_entry.threading.Event", return_value=stop_event):
            import proxy_entry

            proxy_entry.main()

        captured = capsys.readouterr()
        data = json.loads(captured.out.strip())
        assert data["allowed"] == 5
        assert data["blocked"] == 3
        assert data["errors"] == 1
        assert "CONNECT evil.com:443" in data["blocked_log"]
