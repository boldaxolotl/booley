"""Select the Git metadata mode used by worktree relocation."""


def _metadata_flag(relative: bool) -> tuple[str, ...]:
    from booley.runtime.worktree_paths import _git_supports_relative_paths

    if not _git_supports_relative_paths():
        return ()
    return ("--relative-paths" if relative else "--no-relative-paths",)
