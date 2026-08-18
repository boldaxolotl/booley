"""FPGA implementation metrics dataclass + display / source-splitting helpers.

Leaf utilities extracted from :mod:`booley.flows.fpga.flow` (SRP, principle 8):
the normalized :class:`FpgaMetrics` record plus the metric-detail, delta,
display, vlogdefine, source-split, and CSV helpers it depends on. No behavior
change — a pure move + re-export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.flows.clock_timing import ClockTiming, per_clock_to_json


@dataclass
class FpgaMetrics:
    """Normalized FPGA implementation metrics for one config."""

    lut_count: int | None = None
    ff_count: int | None = None
    bram_count: float | None = None
    dsp_count: int | None = None
    wns_ns: float | None = None
    whs_ns: float | None = None
    # Per-clock timing keyed by clock name (Fmax/critical-path are inherently
    # per-clock; the old single scalars were removed). Aggregate wns_ns/whs_ns
    # above stay as honest whole-design worst-case. Empty when Vivado reported no
    # constrained clock (e.g. a purely combinational impl).
    per_clock: dict[str, ClockTiming] = field(default_factory=dict)
    elapsed_s: float = 0.0
    cached: bool = False
    cache_fingerprint: str = ""
    latches: int = 0
    comb_loops: int = 0
    multi_driven: int = 0
    returncode: int = 0
    timed_out: bool = False
    infra_error: str = ""
    # Error tail (log/stderr) surfaced on failure so the real diagnostic reaches
    # the report instead of hiding behind the concise infra_error reason.
    failure_output: str = ""
    # Project-relative path of the persisted full run log (route reports + make
    # log/stderr), written on pass AND fail. Empty when no log was persisted
    # (e.g. a baseline worktree run, whose log dir is destroyed).
    log_path: str = ""
    # Work-dir-relative directories holding this run's artifacts (``build``,
    # ``impl``, ``synth``). The report names directories, not a file inventory
    # — see :mod:`booley.flows.artifacts`.
    dirs: dict[str, str] = field(default_factory=dict)

    @property
    def has_primary_metrics(self) -> bool:
        return self.lut_count is not None and self.ff_count is not None

    @property
    def timing_met(self) -> bool:
        return (
            self.wns_ns is not None
            and self.whs_ns is not None
            and self.wns_ns >= 0.0
            and self.whs_ns >= 0.0
        )

    @property
    def has_critical(self) -> bool:
        return self.latches > 0 or self.comb_loops > 0 or self.multi_driven > 0

    @property
    def passed(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.infra_error
            and self.has_primary_metrics
            and self.timing_met
            and not self.has_critical
        )


def _metrics_detail(
    metrics: FpgaMetrics | None, *, baseline: bool = False
) -> dict[str, Any] | None:
    """Detail dict for one run's metrics; *baseline* suppresses the pointers.

    A ``--baseline`` run happens inside a throwaway git worktree that is
    deleted on context exit, and its report paths were relativized against
    THAT root — so the same relative string, read back against the real
    project root, lands on the CURRENT run's report files. Not a dangling
    path: a silently wrong one, which would make a baseline-vs-current
    comparison read two copies of the same numbers.

    ``log_path`` is already blanked for baseline runs upstream for exactly
    this reason; ``dirs`` is not, so the artifact block is suppressed here
    instead of being filtered key by key.
    """
    if metrics is None:
        return None
    pointers: dict[str, Any] = (
        {}
        if baseline
        else {
            "artifacts": {
                **({"log": metrics.log_path} if metrics.log_path else {}),
                **({"dirs": dict(metrics.dirs)} if metrics.dirs else {}),
            }
        }
    )
    return {
        "lut_count": metrics.lut_count,
        "ff_count": metrics.ff_count,
        "bram_count": metrics.bram_count,
        "dsp_count": metrics.dsp_count,
        "wns_ns": metrics.wns_ns,
        "whs_ns": metrics.whs_ns,
        "per_clock": per_clock_to_json(metrics.per_clock),
        "latches": metrics.latches,
        "comb_loops": metrics.comb_loops,
        "multi_driven": metrics.multi_driven,
        "elapsed_s": metrics.elapsed_s,
        "cached": metrics.cached,
        "cache_fingerprint": metrics.cache_fingerprint or None,
        "failure_output": metrics.failure_output,
        "log_path": metrics.log_path,
        # The pointers under the shared cross-Flow key, so a consumer can read
        # ``artifacts`` from any Booley Flow without a per-Flow special case.
        # ``log_path`` stays as the flat spelling of the same run.log.
        **pointers,
    }


def _delta_pct(current: int | float | None, baseline: int | float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return ((current - baseline) / baseline) * 100.0


def _first_valid_display(configs: list[str], results: dict[str, FpgaMetrics]) -> list[str]:
    for cfg in configs:
        metrics = results[cfg]
        if metrics.lut_count is not None and metrics.wns_ns is not None:
            return [f"{metrics.lut_count:,} LUTs, WNS {metrics.wns_ns:.3f} ns"]
    return []


def _vlogdefine_args(parameters: Any) -> list[str]:
    """Map a resolved Target's ``vlogdefine`` params to Verilog define strings.

    A bool-true define becomes a bare ``NAME``; any other concrete value becomes
    ``NAME=value``; a false/absent default is left undefined (matching Verilog
    ``ifdef`` semantics — an undefined macro, not ``NAME=0``). These feed
    :func:`fpga_edam.build_fpga_edam`'s ``defines`` (decision 8), replacing the
    legacy ``get_config_defines`` lookup.
    """
    defines: list[str] = []
    for name, spec in (parameters or {}).items():
        if not isinstance(spec, dict) or spec.get("paramtype") != "vlogdefine":
            continue
        default = spec.get("default")
        if default is True:
            defines.append(str(name))
        elif default not in (False, None, ""):
            defines.append(f"{name}={default}")
    return defines


def _split_resolved_sources(resolved: Any) -> tuple[list[Path], list[Path], list[Path]]:
    """Split a resolved Target's RTL sources into (.sv, .v, include dirs).

    Shared by :meth:`FpgaImplFlow._prepare_fpga_command` (the real Vivado run)
    and :meth:`FpgaImplFlow._resolve_fpga_summary` (the dry-run preview) off the
    ``.core``-resolved file list (decision 4): each source is an absolute path
    under the FuseSoC build root, so it crosses into the vivado template the
    same way the legacy ``build_file_list`` paths did.
    """
    sv_files: list[Path] = []
    v_files: list[Path] = []
    for f in resolved.rtl_source_files:
        path = f.absolute(resolved.build_root)
        if path.suffix == ".sv":
            sv_files.append(path)
        elif path.suffix == ".v":
            v_files.append(path)
    return sv_files, v_files, list(resolved.rtl_include_dirs)


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _split_csv(value: str) -> list[str]:
    return [part for part in value.split(",") if part]
