"""Resource-bounded GNU Make policy for Verilator simulation-model builds."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import Literal

from booley.core.boundary import as_dict
from booley.flows.memory_admission import job_thread_count
from booley.runtime.cpu_budget import _affinity_cpu_count, _read_text, available_cpu_count
from booley.targets.domain import TargetInspection

LaneKind = Literal["admitted", "unreserved"]
_SHORT_OPTION_CLUSTER_WITH_JOBS = re.compile(r"-[bBdehikLnprRstvwqS]*j\d*")


def authored_make_tokens(inspection: TargetInspection) -> tuple[str, ...]:
    """Return the shell-split words of every authored ``make_options`` item.

    Edalize joins the items into a shell recipe, so one item may hold several words.
    """
    modern = inspection.flow_options.get("make_options")
    legacy = as_dict(inspection.tool_options, default={}) or {}
    tokens: list[str] = []
    for authored in (modern, legacy.get("make_options")):
        if isinstance(authored, Sequence) and not isinstance(authored, (str, bytes)):
            for option in authored:
                try:
                    tokens.extend(shlex.split(str(option)))
                except ValueError:
                    tokens.append(str(option))
    return tuple(tokens)


def _has_jobs_option(tokens: Sequence[str]) -> bool:
    for token in tokens:
        if token in {"-j", "--jobs"} or token.startswith("--jobs="):
            return True
        if _SHORT_OPTION_CLUSTER_WITH_JOBS.fullmatch(token):
            return True
    return False


def verilator_backend_arguments(
    inspection: TargetInspection,
    *,
    lane_kind: LaneKind = "admitted",
) -> tuple[str, ...]:
    """Return only Booley-owned backend options for one Verilator setup.

    An admitted lane's Job shares the cores: its count was fixed at admission.
    No count means no admission root, so no other Job is tracked: use every
    available core. An unreserved lane (mutation variants) builds with ``-j1``.
    """
    if inspection.eda_tool != "verilator" or _has_jobs_option(authored_make_tokens(inspection)):
        return ()
    if lane_kind == "unreserved":
        return ("-j1", "VM_PARALLEL_BUILDS=1")
    threads = job_thread_count()
    make_jobs = (
        available_cpu_count(_affinity_cpu_count, _read_text) if threads is None else threads
    )
    return (f"-j{make_jobs}", "VM_PARALLEL_BUILDS=1")
