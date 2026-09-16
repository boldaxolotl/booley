"""List Python files whose only changes are Sandbox terminology in text."""

import argparse
import ast
import re
import subprocess
from pathlib import Path

_RENAMES = (
    ("session runtimes", "sandboxes"),
    ("session runtime", "sandbox"),
    ("session containers", "sandboxes"),
    ("session container", "sandbox"),
    ("runtime images", "sandbox images"),
    ("runtime image", "sandbox image"),
    ("runtime attachments", "sandbox attachments"),
    ("runtime attachment", "sandbox attachment"),
    ("runtime policy", "sandbox policy"),
    ("runtime", "sandbox"),
    ("session", "sandbox"),
)
_PATTERNS = tuple(
    (re.compile(r"(?<![\w./])" + re.escape(old) + r"(?![\w/])", re.IGNORECASE), new)
    for old, new in _RENAMES
)
_TERM = re.compile(r"session|runtime|sandbox", re.IGNORECASE)


def _canonical_text(value: str) -> str:
    value = value.casefold()
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def terminology_only(before: str, after: str) -> bool:
    """Accept only unchanged Python syntax with renamed terminology in strings."""
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return False
    old_strings = [
        n for n in ast.walk(old_tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    new_strings = [
        n for n in ast.walk(new_tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    if len(old_strings) != len(new_strings):
        return False
    for old, new in zip(old_strings, new_strings, strict=True):
        if old.value != new.value and not (_TERM.search(old.value) or _TERM.search(new.value)):
            return False
        old.value = _canonical_text(old.value)
        new.value = _canonical_text(new.value)
    return ast.dump(old_tree, include_attributes=False) == ast.dump(
        new_tree, include_attributes=False
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", f"{args.base}...HEAD", "--", "src/booley/"],
        text=True,
    ).splitlines()
    for name in paths:
        path = Path(name)
        if path.suffix != ".py" or not path.is_file():
            continue
        before = subprocess.run(
            ["git", "show", f"{args.base}:{name}"], capture_output=True, text=True, check=False
        )
        if before.returncode == 0 and terminology_only(before.stdout, path.read_text()):
            print(name)


if __name__ == "__main__":
    main()
