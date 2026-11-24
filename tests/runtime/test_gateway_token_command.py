"""``BOOLEY_GATEWAY_TOKEN_COMMAND``: running, caching and failing the token command (#2267)."""

from __future__ import annotations

import logging
import math
import shlex
import sys

import pytest
from tests.gateway_command_support import count_runs, write_token_command
from tests.time_budget import scaled

from booley.config import gateway as gateway_config
from booley.runtime import gateway_token

# The gateway policy is honored only inside a Linux container: the command is
# split like a POSIX word list, so these subprocess tests are POSIX-only.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")

_TOKEN = "SYNTHETIC_COMMAND_TOKEN"
_COMMAND_ARG = "SYNTHETIC_COMMAND_ARGUMENT"


class _FakeClock:
    """A monotonic clock a test advances by hand (no sleeps)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("advance", "second_token", "runs"),
    [(-1, _TOKEN, 1), (1, "SYNTHETIC_SECOND", 2)],
    ids=["within-ttl-is-cached", "after-ttl-reruns"],
)
def test_token_command_output_is_cached_for_the_ttl(tmp_path, advance, second_token, runs):
    command, counter = write_token_command(tmp_path, [_TOKEN, "SYNTHETIC_SECOND"])
    clock = _FakeClock()
    cache = gateway_token.GatewayTokenCache(clock=clock)

    first = cache.get(command)
    clock.now += gateway_token.TOKEN_TTL_S + advance
    second = cache.get(command)

    assert (first, second) == (_TOKEN, second_token)
    assert count_runs(counter) == runs


def test_token_command_nonzero_exit_fails_with_stderr_verbatim(tmp_path):
    instruction = "claim a token first:\n  run `gateway-login --device`"
    command, _ = write_token_command(
        tmp_path, ["SYNTHETIC_LEAKED_STDOUT"], stderr=instruction + "\n", exit_code=3
    )

    with pytest.raises(gateway_token.GatewayTokenCommandError) as caught:
        gateway_token.run_token_command(command)

    message = str(caught.value)
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND exited 3" in message
    assert instruction in message
    assert "SYNTHETIC_LEAKED_STDOUT" not in message


def test_token_command_timeout_fails_with_stderr_verbatim():
    # The backgrounded sleep inherits the pipes: only a process-group kill closes
    # them, so stderr is collected only when the kill worked.
    timeout_s = math.ceil(scaled(3))
    command = "sh -c 'echo \"claim a token first\" >&2; echo SYNTHETIC_STDOUT; sleep 30 & wait'"

    with pytest.raises(gateway_token.GatewayTokenCommandError) as caught:
        gateway_token.run_token_command(command, timeout_s=timeout_s)

    message = str(caught.value)
    assert f"BOOLEY_GATEWAY_TOKEN_COMMAND timed out after {timeout_s} s" in message
    assert "claim a token first" in message
    assert "SYNTHETIC_STDOUT" not in message
    assert caught.value.__context__ is None  # TimeoutExpired would carry argv and stdout


@pytest.mark.parametrize(
    ("output", "stderr", "message", "absent"),
    [
        ("", "no token is available yet\n", "printed no token", None),
        (
            "SYNTHETIC_LINE_ONE\nSYNTHETIC_LINE_TWO",
            "",
            "printed an invalid token",
            "SYNTHETIC_LINE",
        ),
        ("SYNTHETIC_T\u00d6KEN", "", "printed an invalid token", "SYNTHETIC_T"),
    ],
    ids=["empty", "multiline", "non-ascii"],
)
def test_token_command_unusable_output_fails_without_echoing_it(
    tmp_path, output, stderr, message, absent
):
    command, _ = write_token_command(tmp_path, [output], stderr=stderr)

    with pytest.raises(gateway_token.GatewayTokenCommandError) as caught:
        gateway_token.run_token_command(command)

    assert f"BOOLEY_GATEWAY_TOKEN_COMMAND {message}" in str(caught.value)
    assert stderr.strip() in str(caught.value)
    if absent:
        assert absent not in str(caught.value)


def test_token_command_missing_executable_fails_naming_the_variable():
    with pytest.raises(gateway_token.GatewayTokenCommandError) as caught:
        gateway_token.run_token_command("synthetic-no-such-gateway-command --secret SYNTHETIC_ARG")

    message = str(caught.value)
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND" in message
    assert "synthetic-no-such-gateway-command" not in message
    assert "SYNTHETIC_ARG" not in message
    assert caught.value.__context__ is None  # the OSError would carry the program name


def test_token_command_token_absent_from_logs_reprs_and_errors(tmp_path, caplog):
    command, _ = write_token_command(tmp_path, [_TOKEN], extra_argument=_COMMAND_ARG)
    (tmp_path / "failing").mkdir()
    failing, _ = write_token_command(
        tmp_path / "failing", [_TOKEN], stderr="refused\n", exit_code=1
    )
    settings = gateway_config.gateway_settings_from_env(
        {
            "BOOLEY_GATEWAY_URL": "https://gateway.example/v1",
            "BOOLEY_GATEWAY_TOKEN_COMMAND": command,
        }
    )
    cache = gateway_token.GatewayTokenCache()
    messages = []

    with caplog.at_level(logging.DEBUG, logger="booley"):
        cache.get(command)
        cache.invalidate()
        cache.get(command)
        with pytest.raises(gateway_token.GatewayTokenCommandError) as caught:
            gateway_token.run_token_command(failing)
        messages.append(str(caught.value))
        with pytest.raises(gateway_config.GatewayConfigError) as bad:
            gateway_config.gateway_settings_from_env(
                {
                    "BOOLEY_GATEWAY_URL": "https://gateway.example/v1",
                    "BOOLEY_GATEWAY_TOKEN_COMMAND": f"'unterminated {_COMMAND_ARG}",
                }
            )
        messages.append(str(bad.value))

    observable = "\n".join([caplog.text, repr(cache), repr(settings), *messages])
    assert caplog.records, "the cache must log what it did"
    assert _TOKEN not in observable
    assert _COMMAND_ARG not in observable
    assert shlex.split(command)[1] not in observable
