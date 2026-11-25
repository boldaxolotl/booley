"""Unit tests for booley.core.boundary: shared boundary coercion and validation.

The regression-critical invariants: every numeric coercion rejects ``bool``
(``isinstance(True, int)`` is ``True``) and rejects non-finite floats (NaN/inf),
and the two flavours differ only in failure mode (default vs. BoundaryError).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_float,
    as_int,
    as_positive_int,
    as_str,
    as_str_list,
    is_str_list,
    require_bool,
    require_bool_value,
    require_dict,
    require_finite_number,
    require_finite_number_value,
    require_int,
    require_list,
    require_opt_str,
    require_positive_int,
    require_sha256_digest,
    require_str,
    require_str_value,
    require_uuid4,
)

NAN = float("nan")
INF = float("inf")
_DIGEST = "sha256:" + "a" * 64
_UUID = "01234567-89ab-4def-8123-456789abcdef"

# (id, call, expected): the result must match in value and in type, so an int
# leaking out of a float helper (or the reverse) fails.
_ACCEPTED: list[tuple[str, Callable[[], Any], Any]] = [
    ("as_dict", lambda: as_dict({"a": 1}), {"a": 1}),
    ("require_dict", lambda: require_dict({"k": "v"}), {"k": "v"}),
    ("as_str", lambda: as_str("hi"), "hi"),
    ("as_str-empty", lambda: as_str(""), ""),
    ("is_str_list", lambda: is_str_list(["a", "b"]), True),
    ("is_str_list-empty", lambda: is_str_list([]), True),
    ("as_str_list-bare-string", lambda: as_str_list("rtl"), ["rtl"]),
    ("as_str_list-list", lambda: as_str_list(["a", "b"]), ["a", "b"]),
    ("as_str_list-filters", lambda: as_str_list(["a", 1, None, "b"]), ["a", "b"]),
    (
        "as_str_list-filtered-empty",
        lambda: as_str_list([1, 2, 3], default=["fallback"]),
        ["fallback"],
    ),
    ("as_str_list-empty-default", lambda: as_str_list([], default=["d"]), ["d"]),
    ("as_str_list-empty", lambda: as_str_list([]), []),
    ("as_str_list-non-list-default", lambda: as_str_list(5, default=["x"]), ["x"]),
    ("as_str_list-none", lambda: as_str_list(None), []),
    ("as_str_list-mapping", lambda: as_str_list({"a": 1}), []),
    ("as_int", lambda: as_int(7), 7),
    ("as_int-truncates", lambda: as_int(3.9), 3),
    ("as_int-string", lambda: as_int("12"), 12),
    ("as_int-float-string", lambda: as_int("3.0"), 3),
    ("as_float-int", lambda: as_float(7), 7.0),
    ("as_float-string", lambda: as_float("2.5"), 2.5),
    ("as_float-int-string", lambda: as_float("4"), 4.0),
    ("as_float", lambda: as_float(3.5), 3.5),
    ("as_positive_int", lambda: as_positive_int(30, default=10), 30),
    ("as_positive_int-field", lambda: as_positive_int(-1, default=1, field="max_sessions"), 1),
    ("require_opt_str-absent", lambda: require_opt_str({}, "engine"), None),
    ("require_opt_str-none", lambda: require_opt_str({"engine": None}, "engine"), None),
    ("require_opt_str", lambda: require_opt_str({"engine": "opensta"}, "engine"), "opensta"),
    ("require_bool-absent", lambda: require_bool({}, "flatten"), False),
    ("require_bool-absent-default", lambda: require_bool({}, "flatten", default=True), True),
    (
        "require_bool-false",
        lambda: require_bool({"flatten": False}, "flatten", default=True),
        False,
    ),
    ("require_bool_value", lambda: require_bool_value(True), True),
    ("require_str", lambda: require_str({"name": "foo"}, "name"), "foo"),
    ("require_str_value-empty", lambda: require_str_value("", allow_empty=True), ""),
    ("require_str_value", lambda: require_str_value("name"), "name"),
    ("require_int", lambda: require_int(7), 7),
    ("require_positive_int", lambda: require_positive_int(3), 3),
    ("require_finite_number-int", lambda: require_finite_number(5, field="wns"), 5.0),
    ("require_finite_number-float", lambda: require_finite_number(2.5, field="wns"), 2.5),
    ("require_sha256_digest", lambda: require_sha256_digest(_DIGEST), _DIGEST),
    ("require_uuid4", lambda: require_uuid4(_UUID), _UUID),
]


@pytest.mark.parametrize(
    ("call", "expected"),
    [pytest.param(call, expected, id=name) for name, call, expected in _ACCEPTED],
)
def test_lenient_coercers_accept(call: Callable[[], Any], expected: Any):
    result = call()

    assert result == expected
    assert type(result) is type(expected)


_DEGRADED_NUMERIC = [True, False, NAN, INF, -INF, "nan", "Infinity", "abc", "", None, [1], {}]
_DEGRADED: list[tuple[Callable[..., Any], Any]] = [
    *((func, value) for func in (as_int, as_float) for value in _DEGRADED_NUMERIC),
    *((as_positive_int, value) for value in (0, -5, True, False, "30", 3.0, None)),
    *((as_dict, value) for value in ([1, 2], "x", None)),
    *((as_str, value) for value in (5, True, None)),
]


@pytest.mark.parametrize(
    ("func", "value"), _DEGRADED, ids=lambda item: getattr(item, "__name__", repr(item))
)
def test_lenient_coercers_degrade_to_default(func: Callable[..., Any], value: Any):
    default = object()

    assert func(value, default=default) is default


@pytest.mark.parametrize("value", ["rtl", ["a", 1], [None], None, {"a": 1}], ids=repr)
def test_is_str_list_rejects(value: Any):
    assert is_str_list(value) is False


_REJECTED: list[tuple[str, Callable[[], Any], str | None]] = [
    ("require_dict-list", lambda: require_dict([1, 2]), None),
    ("require_dict-none", lambda: require_dict(None), None),
    ("require_dict-field", lambda: require_dict("nope", field="tools"), "tools"),
    ("require_list-tuple", lambda: require_list(("a",), field="bindings"), "bindings"),
    ("require_str-missing", lambda: require_str({}, "name"), None),
    ("require_str-empty", lambda: require_str({"name": ""}, "name"), None),
    ("require_str-int", lambda: require_str({"name": 5}, "name"), None),
    ("require_str-bool", lambda: require_str({"name": True}, "name"), None),
    ("require_str_value-int", lambda: require_str_value(1), None),
    ("require_str_value-empty", lambda: require_str_value(""), None),
    ("require_sha256_digest-bare", lambda: require_sha256_digest("a" * 64), None),
    ("require_sha256_digest-upper", lambda: require_sha256_digest("sha256:" + "A" * 64), None),
    ("require_sha256_digest-short", lambda: require_sha256_digest("sha256:abc"), None),
    ("require_sha256_digest-int", lambda: require_sha256_digest(1), None),
    ("require_uuid4-version", lambda: require_uuid4("01234567-89ab-1def-8123-456789abcdef"), None),
    ("require_uuid4-upper", lambda: require_uuid4(_UUID.upper()), None),
    ("require_uuid4-garbage", lambda: require_uuid4("not-a-uuid"), None),
    ("require_uuid4-int", lambda: require_uuid4(1), None),
    # The argv-leak class: TOML `true` must never stringify into "True".
    ("require_opt_str-bool", lambda: require_opt_str({"engine": True}, "engine"), "engine"),
    ("require_opt_str-empty", lambda: require_opt_str({"engine": ""}, "engine"), None),
    ("require_opt_str-int", lambda: require_opt_str({"engine": 5}, "engine"), None),
    (
        "require_opt_str-field",
        lambda: require_opt_str({"engine": 5}, "engine", field="[flows.x] 'engine'"),
        r"\[flows\.x\] 'engine'",
    ),
    # bool("false") is True: the truthiness surprise this helper kills.
    ("require_bool-string", lambda: require_bool({"flatten": "false"}, "flatten"), "flatten"),
    ("require_bool-int", lambda: require_bool({"flatten": 1}, "flatten"), None),
    ("require_bool-none", lambda: require_bool({"flatten": None}, "flatten"), None),
    (
        "require_bool-field",
        lambda: require_bool({"ooc": 1}, "ooc", field="[flows.x] 'ooc'"),
        r"\[flows\.x\] 'ooc'",
    ),
    ("require_bool_value-int", lambda: require_bool_value(1, field="flag"), "flag"),
    *(
        (
            f"require_int-{name}",
            lambda value=value: require_int(value, field="parameter"),
            "parameter",
        )
        for name, value in (("bool", True), ("float", 1.0), ("string", "1"), ("none", None))
    ),
    *(
        (
            f"require_positive_int-{name}",
            lambda value=value: require_positive_int(value, field="revision"),
            "revision",
        )
        for name, value in (
            ("zero", 0),
            ("negative", -1),
            ("bool", True),
            ("float", 1.0),
            ("string", "1"),
            ("none", None),
        )
    ),
    *(
        (
            f"require_finite_number-{name}",
            lambda value=value: require_finite_number(value, field="slack"),
            "slack",
        )
        for name, value in (
            ("bool", True),
            ("nan", NAN),
            ("inf", INF),
            ("string", "3.0"),
            ("none", None),
        )
    ),
    *(
        (
            f"require_finite_number_value-{name}",
            lambda value=value: require_finite_number_value(value, field="threshold"),
            "threshold",
        )
        for name, value in (("bool", True), ("nan", NAN), ("inf", INF), ("string", "3.0"))
    ),
]


@pytest.mark.parametrize(
    ("call", "match"),
    [pytest.param(call, match, id=name) for name, call, match in _REJECTED],
)
def test_strict_validators_reject(call: Callable[[], Any], match: str | None):
    with pytest.raises(BoundaryError, match=match):
        call()


def test_as_dict_returns_a_copy():
    src = {"a": 1}
    out = as_dict(src)
    assert out is not None
    out["b"] = 2

    assert "b" not in src


def test_as_str_list_default_is_copied():
    default = ["d"]
    out = as_str_list(5, default=default)
    out.append("mutated")

    assert default == ["d"]


def test_require_list_returns_the_same_list():
    value = ["a", 1]

    assert require_list(value) is value


def test_require_finite_number_value_keeps_large_int():
    value = 10**1000

    assert require_finite_number_value(value, field="threshold") == value
