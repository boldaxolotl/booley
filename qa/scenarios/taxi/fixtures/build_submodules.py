"""Construct the local companion source fixture; this does not run Booley."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Booley QA",
    "GIT_AUTHOR_EMAIL": "qa@example.invalid",
    "GIT_COMMITTER_NAME": "Booley QA",
    "GIT_COMMITTER_EMAIL": "qa@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(root: Path, *args: str) -> str:
    """Use bounded local Git commands and deterministic identity/configuration."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(root),
            *args,
        ],
        env={**os.environ, **IDENTITY},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def put(root: Path, name: str, content: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def init(root: Path) -> None:
    root.mkdir(parents=True)
    git(root, "init", "--initial-branch=main", "--object-format=sha1")


def commit(root: Path, message: str) -> str:
    git(root, "add", "--all")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def clone(source: Path, target: Path, revision: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    git(target.parent, "clone", "--no-hardlinks", "--no-local", str(source), str(target))
    git(target, "checkout", "--detach", revision)
    git(target, "remote", "remove", "origin")


def modules(root: Path, paths: list[str]) -> None:
    content = "".join(
        f'[submodule "{p}"]\n\tpath = {p}\n\turl = ssh://fixture@example.invalid/{p}.git\n'
        for p in paths
    )
    put(root, ".gitmodules", content)


def leaf_sources(root: Path) -> dict:
    init(root)
    versions = {}
    for label, value in [("A", "12"), ("B", "34")]:
        put(
            root,
            "rtl/leaf.sv",
            f"module qa_leaf(output wire [7:0] value);\nassign value = 8'h{value};\nendmodule\n",
        )
        versions[label] = commit(root, f"leaf {label}")
    return versions


def data_sources(root: Path, leaf: Path, versions: dict) -> dict:
    init(root)
    clone(leaf, root / "nested/leaf", versions["A"])
    modules(root, ["nested/leaf"])
    result = {}
    for label, value in [("A", "1357"), ("B", "2468")]:
        git(root / "nested/leaf", "checkout", "--detach", versions[label])
        put(
            root,
            "rtl/data.sv",
            "module qa_data(output wire [15:0] data, output wire [7:0] leaf);\n"
            f"assign data = 16'h{value};\nqa_leaf child(.value(leaf));\nendmodule\n",
        )
        result[label] = commit(root, f"data {label}")
    return result


def text_sources(root: Path, filename: str, prefix: str) -> dict:
    init(root)
    versions = {}
    for label, version in [("A", 1), ("B", 2)]:
        put(root, filename, f"{prefix}-v{version}\n")
        versions[label] = commit(root, f"{prefix} {label}")
    return versions


def hardware(root: Path) -> None:
    put(
        root,
        "rtl/transport.sv",
        "module qa_transport(input wire [7:0] data_i, output wire [7:0] data_o);\nassign data_o = data_i;\nendmodule\n",
    )
    put(
        root,
        "tb/fixture_tb.sv",
        """module fixture_tb;
parameter EXPECT_DATA = 16'h2468;
parameter EXPECT_LEAF = 8'h34;
wire [15:0] data;
wire [7:0] leaf;
qa_data dut(.data(data), .leaf(leaf));
initial begin
    #1;
    if (data !== EXPECT_DATA || leaf !== EXPECT_LEAF)
        $fatal(1, "DEPENDENCY FAIL DATA=%h LEAF=%h", data, leaf);
    $display("DEPENDENCY PASS DATA=%h LEAF=%h", data, leaf);
    $finish;
end
endmodule
""",
    )
    template = Path(__file__).with_name("fixture.core").read_text(encoding="utf-8")
    put(root, "fixture.core", template)
    put(root, ".gitignore", ".booley_project/\nbuild/\n")


def outer_sources(root: Path, sources: Path, histories: dict) -> dict:
    init(root)
    hardware(root)
    clone(sources / "data", root / "deps/data", histories["data"]["A"])
    clone(sources / "leaf", root / "deps/data/nested/leaf", histories["leaf"]["A"])
    clone(sources / "unselected", root / "deps/unselected", histories["unselected"]["A"])
    modules(root, ["deps/data", "deps/unselected"])
    pair = root / ".booley_project"
    init(pair)
    clone(sources / "control", pair / "deps/control", histories["control"]["A"])
    modules(pair, ["deps/control"])
    outer, paired = {}, {}
    for label in ["A", "B"]:
        for name in ["data", "unselected"]:
            git(root / "deps" / name, "checkout", "--detach", histories[name][label])
        git(root / "deps/data/nested/leaf", "checkout", "--detach", histories["leaf"][label])
        git(pair / "deps/control", "checkout", "--detach", histories["control"][label])
        paired[label] = commit(pair, f"project control {label}")
        outer[label] = commit(root, f"companion {label}")
    return {"outer": outer, "paired": paired}


def populate(root: Path) -> dict:
    """Freeze complete A/B object identities within the attempt-owned directory."""
    sources = root / "sources"
    histories = {"leaf": leaf_sources(sources / "leaf")}
    histories["data"] = data_sources(sources / "data", sources / "leaf", histories["leaf"])
    histories["control"] = text_sources(sources / "control", "fixture-control.txt", "control")
    histories["unselected"] = text_sources(sources / "unselected", "harmless.txt", "unselected")
    histories.update(outer_sources(root / "companion", sources, histories))
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }
    manifest = {"object_format": "sha1", **histories, "files": hashes}
    put(root, "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def construct(root: Path) -> dict:
    """Retain a complete fixture, cleaning only this attempt's state on failure."""
    root.mkdir(parents=True, exist_ok=False)
    complete = False
    try:
        manifest = populate(root)
        complete = True
        return manifest
    finally:
        if not complete:
            shutil.rmtree(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New operator-owned directory; must not exist")
    args = parser.parse_args()
    construct(args.output.resolve())


if __name__ == "__main__":
    main()
