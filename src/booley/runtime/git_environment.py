"""Inherited Git environment for commands that select an explicit checkout."""

import os

REPOSITORY_SELECTION_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_ATTR_SOURCE",
    }
)


def inherited_git_environment() -> dict[str, str]:
    """Copy inherited settings without checkout or attribute-source redirection.

    Callers apply their explicit overrides afterwards, preserving private indexes
    and object stores intentionally selected for one operation.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key not in REPOSITORY_SELECTION_VARIABLES
    }
