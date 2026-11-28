"""The shared Docker CLI inspect call refuses answers it cannot trust."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.fake_docker_executable import install_fake_docker

from booley.core.boundary import require_bool_value, require_list, require_str_value


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, output: str, detail: str | None
) -> None:
    from booley.core import docker_cli

    docker = install_fake_docker(tmp_path, monkeypatch, {"network inspect": (code, output)})
    read = docker_cli.inspect_labels if "booley" in output else docker_cli.inspect

    with pytest.raises(docker_cli.InspectError) as caught:
        read(docker.runner, "network", "booley-net", timeout=30)

    failed = caught.value.result
    assert (None if failed is None else docker_cli.failure_detail(failed)) == detail


def _value(require: Callable[..., object]) -> Callable[[Any, Any], object]:
    return lambda cli, run: cli.inspect_value(
        run, "image", "img", timeout=30, template="{{json .X}}", require=require
    )


def _label(cli: Any, run: Any) -> object:
    return cli.inspect_label(run, "image", "img", "k", timeout=30)


def _exists(cli: Any, run: Any) -> object:
    return cli.exists(run, "image", "img", timeout=30)


def _registry(template: str | None) -> Callable[[Any, Any], object]:
    return lambda cli, run: cli.inspect_registry_image(run, "img", timeout=30, template=template)


_VALUE_ARGS = ["image", "inspect", "img", "--format", "{{json .X}}"]
_LABEL_ARGS = ["image", "inspect", "img", "--format", '{{json (index .Config.Labels "k")}}']
_EXISTS_ARGS = ["image", "inspect", "img"]
_REGISTRY_ARGS = ["buildx", "imagetools", "inspect", "img"]
_FORMAT_ARGS = [*_REGISTRY_ARGS, "--format", "{{json .Manifest}}"]
_RAW_ARGS = [*_REGISTRY_ARGS, "--raw"]


@pytest.mark.parametrize(
    ("code", "output", "read", "argv", "expected"),
    [
        pytest.param(0, "true", _value(require_bool_value), _VALUE_ARGS, True, id="bool"),
        pytest.param(0, '["a"]', _value(require_list), _VALUE_ARGS, ["a"], id="list"),
        pytest.param(0, "7", _value(require_str_value), _VALUE_ARGS, "malformed", id="type"),
        pytest.param(0, "null", _label, _LABEL_ARGS, None, id="label-null"),
        pytest.param(0, '""', _label, _LABEL_ARGS, None, id="label-empty"),
        pytest.param(0, '"v"', _label, _LABEL_ARGS, "v", id="label"),
        pytest.param(0, "{}", _exists, _EXISTS_ARGS, True, id="exists"),
        pytest.param(1, "gone", _exists, _EXISTS_ARGS, False, id="absent"),
        pytest.param(
            0,
            '{"a": 1}',
            _registry("{{json .Manifest}}"),
            _FORMAT_ARGS,
            {"a": 1},
            id="registry-format",
        ),
        pytest.param(0, '{"a": 1}', _registry(None), _RAW_ARGS, {"a": 1}, id="registry-raw"),
    ],
)
def test_inspect_value_returns_the_validated_template_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    output: str,
    read: Callable[[Any, Any], object],
    argv: list[str],
    expected: object,
) -> None:
    from booley.core import docker_cli

    docker = install_fake_docker(tmp_path, monkeypatch, {"inspect": (code, output)})

    try:
        observed = read(docker_cli, docker.runner)
    except docker_cli.InspectError as exc:
        assert exc.result is None
        observed = "malformed"

    assert observed == expected
    assert docker.calls == [argv]
