"""Compatibility import for standalone Job artifact-cache callers.

Dashboard refreshes use JobObserver, which owns cache lifetime and sequencing.
"""

from booley.runtime.job_observation._artifacts import JobArtifactCache

__all__ = ["JobArtifactCache"]
