"""Replaying a record's frozen Test Runs as a new run re-plans the same identity (#4370)."""

import argparse
import shlex

import pytest

from booley.flows.sim.cli import SimArguments
from booley.flows.sim.record import codec, planning
from tests.flows.sim.simulation_record_boundary import PASS_PROCESS, run_simulation_record_sim
from tests.flows.sim.test_cocotb import _CORE_FILES

_HDL = '[sim_0]\ntests = ["reset", "wrap"]\n'
_VARIANTS = _HDL + 'variants.reset = {default="short", short=[], soak=["+n=9"]}\n'
# The default suite batches as [a, c], [b], so its replay has to name each run.
_COCOTB = """[ccfg]
tests = ["a", "b", "c"]
variants.a = {default="one", one={env={M="A"}}, shared={env={M="C"}}}
variants.b = {default="two", two={env={M="B"}}, shared={env={M="C"}}}
variants.c = {default="one", one={env={M="A"}}}
"""
_CASES = {
    "default-suite": (_HDL, {}),
    "explicit-reordered": (_HDL, {"test": ["wrap", "reset"]}),
    "seed-random": (_HDL, {"seed": "random"}),
    "unnamed-seed-random": ("", {"seed": "random"}),
    "variants-and-count": (_VARIANTS, {"test": ["reset+soak", "reset@x2", "wrap"]}),
    "cocotb-default-suite": (_COCOTB, {}),
    "cocotb-batches": (_COCOTB, {"test": ["a+shared", "c", "b+shared", "a", "c@x2"]}),
}


def _record_and_replan(tmp_path, monkeypatch, toml, request):
    """Plan a record, then a new run from its frozen options; return both and the options."""
    target = "ccfg" if toml.startswith("[ccfg]") else "sim_0"
    files = {**_CORE_FILES, ".booley/tests.toml": toml}

    def record(number, selection):
        # The runs' grades are irrelevant: only the planned manifests are compared.
        run = run_simulation_record_sim(
            tmp_path,
            monkeypatch,
            {"sim_0": []},
            PASS_PROCESS,
            files=files,
            target=target,
            **selection,
        )
        path = tmp_path / f"reports/sim/{number}/targets/{target}/record/manifest.json"
        return run.flow, path, codec.decode_simulation_record_manifest(path.read_bytes())

    flow, path, stored = record(1, request)
    # Private seam: publicly, only a resume refusal prints these options until #4371.
    options = flow._frozen_test_run_options(stored, target)
    parser = argparse.ArgumentParser()
    SimArguments.add_args(parser)
    replay = parser.parse_args(shlex.split(options))
    replanned = record(2, {"test": replay.test, "seed": replay.seed})[2]
    return stored, replanned, path, options


@pytest.mark.parametrize("case", _CASES)
def test_replaying_the_frozen_test_runs_replans_the_same_identity(tmp_path, monkeypatch, case):
    stored, replanned, _, options = _record_and_replan(tmp_path, monkeypatch, *_CASES[case])
    # This case pins the suite flag only while its replay names the runs.
    assert case != "cocotb-default-suite" or "--test=" in options
    planning.verify_workload(stored, replanned)  # raising is a refused resume
