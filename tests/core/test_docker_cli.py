"""The shared Docker CLI inspect call refuses answers it cannot trust."""

from pathlib import Path

import pytest
from tests.fake_docker_executable import install_fake_docker


@pytest.mark.parametrize(
    ("code", "output", "detail"),
    [
        pytest.param(1, "daemon busy", "daemon busy", id="non-zero"),
        pytest.param(0, "not json", None, id="non-json"),
        pytest.param(0, "[]", None, id="not-an-object"),
        pytest.param(0, '{"booley.role": 7}', None, id="non-string-label"),
    ],
)
def test_inspect_raises_the_typed_error_for_untrusted_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    output: str,
    detail: str | None,
) -> None:
    from booley.core import docker_cli

    docker = install_fake_docker(tmp_path, monkeypatch, {"network inspect": (code, output)})
    read = docker_cli.inspect_labels if "booley" in output else docker_cli.inspect

    with pytest.raises(docker_cli.InspectError) as caught:
        read(docker.runner, "network", "booley-net", timeout=30)

    failed = caught.value.result
    assert (None if failed is None else docker_cli.failure_detail(failed)) == detail
