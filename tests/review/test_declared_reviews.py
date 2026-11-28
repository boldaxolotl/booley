"""Declared Reviews in ``reviews.toml``: loading, resolving and reading them (#3271)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.runtime.test_commit_export import (
    commit_all,
    git,
    make_versioned_booley_directory,
    new_repository,
    write,
)

if TYPE_CHECKING:
    from booley.review.review_resolution import DeclaredReview

REVIEWER_DOC = Path(__file__).resolve().parents[2] / "docs" / "user" / "REVIEWER.md"
SIM_CORE = """\
CAPI=2:
name: ::sim_core:0
filesets:
  rtl:
    files: [rtl/a.sv, rtl/b.sv, rtl/gen/c.sv]
    file_type: systemVerilogSource
  tb:
    files: [tb/tb.sv]
    file_type: systemVerilogSource
    tags: [tb]
targets:
  sim_core: &target
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: top
  sim_fifo: *target
"""
SIM_CORE_ID = "::sim_core:0#sim_core"
FILES = ("rtl/a.sv", "rtl/b.sv", "rtl/gen/c.sv", "tb/tb.sv", "docs/spec.md", "fw/boot.c")


def write_project(root: Path, reviews: str | bytes | None) -> Path:
    """A Project with Targets ``sim_core`` and ``sim_fifo`` and two files in no fileset."""
    root.mkdir(parents=True, exist_ok=True)
    write(root / ".booley" / "booley.toml", "[project]\n")
    write(root / "sim.core", SIM_CORE)
    for name in FILES:
        write(root / name, "module m; endmodule\n")
    if reviews is not None:
        data = reviews if isinstance(reviews, bytes) else reviews.encode()
        (root / ".booley" / "reviews.toml").write_bytes(data)
    return root


def review(
    role: str, focus: str, resolve: str, files: set[str], spec: str | None = "docs/spec.md"
) -> DeclaredReview:
    from booley.review.review_resolution import DeclaredReview

    return DeclaredReview(role, focus, spec, resolve, frozenset(files))  # type: ignore[arg-type]


def test_tables_defaults_and_path_entries_resolve(tmp_path: Path) -> None:
    from booley.review.review_resolution import load_declared_reviews

    root = write_project(
        tmp_path,
        """\
[defaults.review]
rtl = ["bugs"]
tb = ["quality"]
resolve = "minor"

[sim_core.review]
rtl = ["spec", "bugs"]
spec = "docs/spec.md"
exclude = ["rtl/gen/*.sv"]

[[sim_core.review.paths]]
paths = ["rtl/**"]
rtl = ["security"]
resolve = "critical"
""",
    )

    declared = load_declared_reviews(root)

    rtl = {"rtl/a.sv", "rtl/b.sv"}
    assert declared.reviews == {
        SIM_CORE_ID: {
            review("rtl", "spec", "minor", rtl),
            review("rtl", "bugs", "minor", rtl),
            review("tb", "quality", "minor", {"tb/tb.sv"}),
            review("rtl", "security", "critical", rtl),
        }
    }
    (table,) = declared.tables
    assert (table.target, table.rtl, table.tb, table.resolve) == (
        "sim_core",
        ("spec", "bugs"),
        ("quality",),
        "minor",
    )
    assert table.inherited == {"tb", "resolve"}
    assert declared.path == root / ".booley" / "reviews.toml"


def test_unwritten_keys_take_built_in_defaults_and_entries_carry_their_own(
    tmp_path: Path,
) -> None:
    from booley.review.review_resolution import load_declared_reviews

    root = write_project(
        tmp_path,
        """\
[defaults.review]
exclude = ["rtl/gen/*.sv"]

[sim_core.review]
rtl = ["bugs"]

[[sim_core.review.paths]]
paths = ["tb/*.sv", "rtl/a.sv"]
rtl = ["spec"]
tb = ["quality"]
spec = "docs/spec.md"
""",
    )

    declared = load_declared_reviews(root)

    assert declared.reviews == {
        SIM_CORE_ID: {
            review("rtl", "bugs", "major", {"rtl/a.sv", "rtl/b.sv"}, spec=None),
            review("rtl", "spec", "major", {"rtl/a.sv"}),
            review("tb", "quality", "major", {"tb/tb.sv"}),
        }
    }


def test_include_headers_are_reviewed_as_rtl(tmp_path: Path) -> None:
    from booley.review.review_resolution import load_declared_reviews

    root = write_project(
        tmp_path,
        '[sim_core.review]\nrtl = ["bugs"]\n[[sim_core.review.paths]]\n'
        'paths = ["rtl/*.svh"]\nrtl = ["security"]\n',
    )
    write(root / "rtl" / "defs.svh", "`define W 8\n")
    core = root / "sim.core"
    core.write_bytes(
        core.read_bytes().replace(
            b"files: [rtl/a.sv,", b"files: [rtl/a.sv, {rtl/defs.svh: {is_include_file: true}},"
        )
    )

    declared = load_declared_reviews(root)

    rtl = {"rtl/a.sv", "rtl/b.sv", "rtl/gen/c.sv", "rtl/defs.svh"}
    assert declared.reviews == {
        SIM_CORE_ID: {
            review("rtl", "bugs", "major", rtl, spec=None),
            review("rtl", "security", "major", {"rtl/defs.svh"}, spec=None),
        }
    }


def test_no_reviews_toml_declares_nothing(tmp_path: Path) -> None:
    from booley.review.review_resolution import load_declared_reviews

    declared = load_declared_reviews(write_project(tmp_path, None))

    assert declared.tables == ()
    assert declared.reviews == {}


FOCUS = '[sim_core.review]\nrtl = ["bugs"]\n'
ENTRY = '[[sim_core.review.paths]]\nrtl = ["bugs"]\n'
RTL_FOCUSES = "spec, bugs, protocol, security, optimization, code_style"
ERRORS = {
    "unknown-target": (
        '[nope.review]\nrtl = ["bugs"]\n',
        ["[nope.review] Unknown target 'nope'; selectable Targets: sim_core, sim_fifo"],
    ),
    "unknown-key-in-table": (
        FOCUS + 'severity = "x"\n',
        [
            "[sim_core.review] severity: unknown key; use one of: rtl, tb, spec, resolve, exclude, paths"
        ],
    ),
    "unknown-key-under-target": (
        "[sim_core]\nlint = 1\n" + FOCUS,
        ["[sim_core] lint: unknown key; use one of: review"],
    ),
    "unknown-focus": (
        '[sim_core.review]\nrtl = ["bugs", "speed"]\n',
        ["[sim_core.review] rtl: unknown focus 'speed'; use one of: " + RTL_FOCUSES],
    ),
    "focus-of-the-other-role": (
        FOCUS + 'tb = ["quality", "bugs"]\n',
        [
            "[sim_core.review] tb: 'bugs' is an RTL focus, not a testbench focus; use one of: quality"
        ],
    ),
    "spec-focus-without-spec": (
        '[sim_core.review]\nrtl = ["spec", "bugs"]\n',
        ["[sim_core.review] spec: the 'spec' focus needs a spec path; set spec"],
    ),
    "spec-path-missing": (
        FOCUS + 'spec = "docs/nope.md"\n',
        ["[sim_core.review] spec: 'docs/nope.md' is not a file of the Project"],
    ),
    "invalid-resolve": (
        FOCUS + 'resolve = "blocker"\n',
        ["[sim_core.review] resolve: must be one of: critical, major, minor"],
    ),
    "exclude-matches-nothing": (
        FOCUS + 'exclude = ["rtl/*.v"]\n',
        ["[sim_core.review] exclude: glob 'rtl/*.v' matches no file of the Target"],
    ),
    "paths-glob-matches-nothing": (
        FOCUS + ENTRY + 'paths = ["rtl/*.v"]\n',
        ["[[sim_core.review.paths]] #1 paths: glob 'rtl/*.v' matches no file of the Target"],
    ),
    "glob-outside-the-target": (
        FOCUS + ENTRY + 'paths = ["rtl/*.sv", "docs/*"]\n',
        [
            "[[sim_core.review.paths]] #1 paths: glob 'docs/*' matches files outside the Target: docs/spec.md"
        ],
    ),
    "glob-outside-the-target-names-three": (
        FOCUS + 'exclude = ["**"]\n',
        [
            "[sim_core.review] exclude: glob '**' matches files outside the Target: "
            ".booley/booley.toml, .booley/reviews.toml, docs/spec.md and 2 more"
        ],
    ),
    "glob-matches-only-the-other-role": (
        FOCUS + ENTRY + 'paths = ["tb/*"]\n',
        ["[[sim_core.review.paths]] #1 paths: glob 'tb/*' matches no RTL file of the Target"],
    ),
    "entry-focus-without-files-of-its-role": (
        FOCUS + ENTRY + 'paths = ["rtl/*.sv"]\ntb = ["quality"]\n',
        [
            "[[sim_core.review.paths]] #1 paths: no glob matches a testbench file of the Target, "
            "but the entry carries tb focuses"
        ],
    ),
    "spec-path-with-a-nul": (
        FOCUS + 'spec = "docs/\\u0000spec.md"\n',
        ["[sim_core.review] spec: 'docs/\\x00spec.md' is not a file of the Project"],
    ),
    "glob-matches-firmware": (
        FOCUS + ENTRY + 'paths = ["fw/*"]\n',
        [
            "[[sim_core.review.paths]] #1 paths: glob 'fw/*' matches files outside the Target: fw/boot.c"
        ],
    ),
    "table-without-a-focus": (
        '[sim_core.review]\nresolve = "minor"\n',
        ["[sim_core.review] rtl: declares no focus; set rtl or tb"],
    ),
    "entry-without-paths": (
        FOCUS + ENTRY,
        ["[[sim_core.review.paths]] #1 paths: is required: list the globs the entry covers"],
    ),
    "entry-with-empty-paths": (
        FOCUS + ENTRY + "paths = []\n",
        ["[[sim_core.review.paths]] #1 paths: must not be empty"],
    ),
    "entry-without-a-focus": (
        FOCUS + '[[sim_core.review.paths]]\npaths = ["rtl/*"]\n',
        ["[[sim_core.review.paths]] #1 rtl: declares no focus; set rtl or tb"],
    ),
    "entry-spec-focus-without-spec": (
        FOCUS + '[[sim_core.review.paths]]\npaths = ["rtl/*"]\nrtl = ["spec"]\n',
        ["[[sim_core.review.paths]] #1 spec: the 'spec' focus needs a spec path; set spec"],
    ),
    "entry-spec-path-missing": (
        FOCUS + ENTRY + 'paths = ["rtl/*"]\nspec = "docs/nope.md"\n',
        ["[[sim_core.review.paths]] #1 spec: 'docs/nope.md' is not a file of the Project"],
    ),
    "wrong-value-types": (
        '[sim_core.review]\nrtl = "bugs"\nspec = 3\nexclude = [1]\n',
        [
            "[sim_core.review] rtl: must be a list of strings",
            "[sim_core.review] spec: must be a non-empty path string",
            "[sim_core.review] exclude: must be a list of strings",
        ],
    ),
    "review-is-not-a-table": (
        "[sim_core]\nreview = 1\n",
        ["[sim_core.review] must be a table"],
    ),
    "paths-is-not-an-array-of-tables": (
        FOCUS + "paths = 1\n",
        ["[sim_core.review] paths: must be an array of tables"],
    ),
    "entry-is-not-a-table": (
        FOCUS + 'paths = ["rtl/*"]\n',
        ["[[sim_core.review.paths]] #1 must be a table"],
    ),
    "target-is-not-a-table": ("sim_core = 1\n", ["[sim_core] must be a table"]),
    "error-in-defaults-is-reported-once": (
        '[defaults.review]\nrtl = ["bugs", "speed"]\n[sim_core.review]\n[sim_fifo.review]\n',
        ["[defaults.review] rtl: unknown focus 'speed'; use one of: " + RTL_FOCUSES],
    ),
    "default-exclude-checked-for-each-target-it-fills": (
        '[defaults.review]\nexclude = ["docs/*"]\n' + FOCUS,
        [
            "[sim_core.review] exclude: glob 'docs/*' matches files outside the Target: "
            "docs/spec.md (from [defaults.review])"
        ],
    ),
    "default-spec-path-missing-is-reported-once": (
        '[defaults.review]\nrtl = ["bugs"]\nspec = "docs/nope.md"\n[sim_core.review]\n[sim_fifo.review]\n',
        ["[defaults.review] spec: 'docs/nope.md' is not a file of the Project"],
    ),
    "spec-path-missing-beside-a-valid-default": (
        '[defaults.review]\nrtl = ["bugs"]\nspec = "docs/spec.md"\n'
        '[sim_core.review]\nspec = "docs/nope.md"\n[sim_fifo.review]\n',
        ["[sim_core.review] spec: 'docs/nope.md' is not a file of the Project"],
    ),
    "defaults-is-not-a-table-of-review": (
        "[defaults.sim]\nrtl = 1\n",
        ["[defaults] sim: unknown key; use one of: review"],
    ),
    "defaults-review-is-not-a-table": (
        "[defaults]\nreview = 1\n",
        ["[defaults.review] must be a table"],
    ),
    "not-toml": ("rtl = [\n", ["is not valid TOML: Invalid value (at end of document)"]),
    "not-utf-8": (
        b"\xff\xfe",
        ["is not UTF-8: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"],
    ),
}


@pytest.mark.parametrize("name", ERRORS)
def test_load_error_names_table_and_key(tmp_path: Path, name: str) -> None:
    from booley.review.declared_reviews import ReviewsFileError
    from booley.review.review_resolution import load_declared_reviews

    text, expected = ERRORS[name]
    root = write_project(tmp_path, text)

    with pytest.raises(ReviewsFileError) as raised:
        load_declared_reviews(root)

    assert [error.render() for error in raised.value.errors] == expected
    assert str(raised.value).startswith("reviews.toml has ")


def commit_reviews(layout: str, tmp_path: Path, committed: str, live: str) -> tuple[Path, str]:
    """A repository whose HEAD holds *committed* ``reviews.toml`` and whose files hold *live*."""
    if layout == "in-tree":
        repo = new_repository(tmp_path / "repo")
        write(repo / ".booley" / "booley.toml", "[project]\n")
        write(repo / "rtl.v", "rtl\n")
        write(repo / ".booley" / "reviews.toml", committed)
        head = commit_all(repo, "head")
        write(repo / ".booley" / "reviews.toml", live)
        return repo, head
    repo, booley = make_versioned_booley_directory(tmp_path, layout)
    write(booley / "reviews.toml", committed)
    commit_all(booley, "reviews")
    write(booley / "reviews.toml", live)
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("layout", ["in-tree", "standalone", "paired-checkout"])
def test_effective_reviews_of_a_commit_read_from_git(tmp_path: Path, layout: str) -> None:
    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import TargetReviews, declared_reviews_at

    committed = '[defaults.review]\nresolve = "minor"\n[sim_core.review]\nrtl = ["bugs"]\n'
    repo, head = commit_reviews(
        layout, tmp_path, committed, '[sim_core.review]\ntb = ["quality"]\n'
    )

    declared = declared_reviews_at(repo, resolve_measurement_key(repo, head))

    assert declared == (
        TargetReviews("sim_core", ("bugs",), (), None, "minor", (), (), frozenset({"resolve"})),
    )


def test_a_commit_without_reviews_toml_declares_nothing(tmp_path: Path) -> None:
    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import declared_reviews_at

    repo = new_repository(tmp_path / "repo")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    head = commit_all(repo, "head")

    assert declared_reviews_at(repo, resolve_measurement_key(repo, head)) == ()


def test_a_versioned_commit_needs_a_versioned_booley_directory(tmp_path: Path) -> None:
    from dataclasses import replace

    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import declared_reviews_at

    repo, head = commit_reviews("in-tree", tmp_path, FOCUS, FOCUS)
    keyed = replace(resolve_measurement_key(repo, head), booley_commit=head)

    with pytest.raises(ValueError, match="come from a Versioned Booley Directory"):
        declared_reviews_at(repo, keyed)


def test_reviewer_doc_example_loads(tmp_path: Path) -> None:
    from booley.review.review_resolution import load_declared_reviews

    text = REVIEWER_DOC.read_text(encoding="utf-8")
    section = text.split("## Declare reviews in `reviews.toml`", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```toml\n(.*?)```", section, flags=re.DOTALL)
    assert blocks

    for number, block in enumerate(blocks):
        declared = load_declared_reviews(write_project(tmp_path / str(number), block))
        assert declared.reviews, block


IN_TREE_REVIEWS = '[sim_core.review]\nrtl = ["bugs"]\nspec = "docs/spec.md"\n'
PAIRED_REVIEWS = IN_TREE_REVIEWS.replace("docs/spec.md", ".booley/spec.md")
REVIEWS_PATH = ".booley/reviews.toml"
IN_TREE_CASES = {
    "unchanged": ({}, (), ()),
    "reviews-edited": (
        {REVIEWS_PATH: IN_TREE_REVIEWS + 'resolve = "minor"\n'},
        (),
        ("reviews.toml",),
    ),
    "spec-edited": ({"docs/spec.md": "two\n"}, (), ("docs/spec.md",)),
    "spec-target-edited": ({"docs/real.md": "two\n"}, ("docs/real.md",), ("docs/spec.md",)),
    "unreadable-at-base": ({}, ("broken",), ("reviews.toml",)),
}


@pytest.mark.parametrize("case", IN_TREE_CASES)
def test_review_rules_changes_since_the_base(tmp_path: Path, case: str) -> None:
    """AC3: a changed reviews.toml or declared spec (even through a symlink) is reported."""
    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import review_rules_changes

    edits, flags, expected = IN_TREE_CASES[case]
    if "docs/real.md" in flags and sys.platform == "win32":
        pytest.skip("needs symlinks")
    repo = new_repository(tmp_path / "repo")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    write(repo / "rtl.v", "rtl\n")
    reviews = "[sim_core\n" if "broken" in flags else IN_TREE_REVIEWS
    write(repo / REVIEWS_PATH, reviews)
    write(
        repo / "docs" / "spec.md" if "docs/real.md" not in flags else repo / "docs" / "real.md",
        "one\n",
    )
    if "docs/real.md" in flags:
        (repo / "docs" / "spec.md").symlink_to("real.md")
    base = commit_all(repo, "base")
    write(repo / REVIEWS_PATH, IN_TREE_REVIEWS)
    for path, text in edits.items():
        write(repo / path, text)
    head = commit_all(repo, "head")

    changed = review_rules_changes(repo, resolve_measurement_key(repo, head), base)

    assert changed == expected


@pytest.mark.parametrize(
    ("case", "edit", "expected"),
    [
        (
            "reviews-edited",
            {"reviews.toml": PAIRED_REVIEWS + 'resolve = "minor"\n'},
            ("reviews.toml",),
        ),
        ("spec-edited", {"spec.md": "two\n"}, (".booley/spec.md",)),
    ],
)
def test_review_rules_changes_compare_a_versioned_booley_directory_with_its_rules_base(
    tmp_path: Path, case: str, edit: dict[str, str], expected: tuple[str, ...]
) -> None:
    """AC3: rules in a Versioned Booley Directory are compared there, not in the RTL repository."""
    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import review_rules_changes

    checkout, booley = make_versioned_booley_directory(tmp_path, "paired-checkout")
    original = tmp_path / "main" / ".booley"
    write(original / "reviews.toml", PAIRED_REVIEWS)
    write(original / "spec.md", "one\n")
    commit_all(original, "rules base")
    git(booley, "merge", "-q", "--no-edit", "main")
    for name, text in edit.items():
        write(booley / name, text)
        git(booley, "add", name)
    commit_all(booley, case, staged_only=True)
    head = git(checkout, "rev-parse", "HEAD")

    changed = review_rules_changes(checkout, resolve_measurement_key(checkout, head), head)

    assert changed == expected


@pytest.mark.parametrize("spec", ["docs/spec.md", "vendor/spec.md"])
def test_review_rules_changes_read_symlinks_and_submodules_as_the_reviewer_does(
    tmp_path: Path, spec: str
) -> None:
    """AC3: a symlinked reviews.toml parses, a retargeted link and a moved submodule pin count."""
    from booley.measurement.run_key import resolve_measurement_key
    from booley.review.declared_reviews import review_rules_changes

    if sys.platform == "win32":
        pytest.skip("needs symlinks")
    sub = new_repository(tmp_path / "sub")
    write(sub / "spec.md", "one\n")
    sub_one = commit_all(sub, "one")
    write(sub / "spec.md", "two\n")
    sub_two = commit_all(sub, "two")
    repo = new_repository(tmp_path / "repo")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    write(repo / "rtl.v", "rtl\n")
    write(repo / "config" / "reviews.toml", IN_TREE_REVIEWS.replace("docs/spec.md", spec))
    (repo / REVIEWS_PATH).symlink_to("../config/reviews.toml")
    write(repo / "docs" / "old.md", "old\n")
    write(repo / "docs" / "new.md", "new\n")
    (repo / "docs" / "alias.md").symlink_to("old.md")
    (repo / "docs" / "spec.md").symlink_to("alias.md")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    git(repo / "vendor", "checkout", "-q", sub_one)
    base = commit_all(repo, "base")
    (repo / "docs" / "alias.md").unlink()
    (repo / "docs" / "alias.md").symlink_to("new.md")
    git(repo / "vendor", "checkout", "-q", sub_two)
    head = commit_all(repo, "head")

    changed = review_rules_changes(repo, resolve_measurement_key(repo, head), base)

    assert changed == (spec,)
