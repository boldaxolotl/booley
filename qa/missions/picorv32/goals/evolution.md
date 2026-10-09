# Goalset: evolution

Implement RV32 Zbb PCPI from the integrated Dhrystone result. The public ISA
manual is copied into `docs/riscv-isa-unprivileged.html` before entry; review
that file. Implement ANDN, ORN, XNOR, CLZ, CTZ, CPOP, MIN, MINU, MAX, MAXU,
SEXT.B, SEXT.H, ZEXT.H, ROL, ROR, RORI, ORC.B and REV8. Add `ENABLE_ZBB`,
default 0, consistently in core, AXI and WB, using registered internal PCPI
with fixed one-cycle response.

Author directed wrapper tests and ordinary Targets named below. `sim_axi_zbb`
uses `testbench`, which instantiates `picorv32_axi`. Register complete enabled
suites for core/AXI/WB. The disabled test arms a distinct MMIO marker immediately
before its first Zbb encoding and requires that illegal-instruction trap.
Keep existing core `main`/`axi`, WB `wb`, Dhrystone `dhry` tests and genuinely
execute them. Keep Target definitions and owned test tables for future regressions.
Mutation steering covers Zbb decode, result generation, PCPI handshake and enable
gating. Implementation assets are `picorv32.v`, `testbench.v`, `testbench_wb.v`,
`Makefile`, and new `tests/zbb.S`, plus needed Project Target/test definitions.
With Vivado configured, additionally enter `fpga.json` as ad-hoc Goals.

## Goals

```json
[
  {
    "family": "elab",
    "target": "sim_core_zbb",
    "origin": "evolution"
  },
  {
    "family": "elab",
    "target": "sim_axi_zbb",
    "origin": "evolution"
  },
  {
    "family": "elab",
    "target": "sim_wb_zbb",
    "origin": "evolution"
  },
  {
    "family": "elab",
    "target": "sim_zbb_disabled",
    "origin": "evolution"
  },
  {
    "family": "lint",
    "target": "lint_core_zbb",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_core",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_wb",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_dhry_checked",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_core_zbb",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_axi_zbb",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_wb_zbb",
    "origin": "evolution"
  },
  {
    "family": "sim",
    "target": "sim_zbb_disabled",
    "origin": "evolution"
  },
  {
    "family": "mutation",
    "target": "sim_core_zbb",
    "scope": [
      "picorv32.v"
    ],
    "min_detected": 14,
    "total": 15,
    "origin": "evolution"
  },
  {
    "family": "synth",
    "target": "synth_core_zbb",
    "baseline": "synth_core",
    "thresholds": {
      "cell_count_increase_at_most": "11%",
      "critical_path_ps_increase_at_most": "3%"
    },
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_bugs",
    "verdict": "clean",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_protocol",
    "verdict": "done",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_spec",
    "verdict": "done",
    "spec": "docs/riscv-isa-unprivileged.html",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_code_style",
    "verdict": "done",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_optimization",
    "verdict": "done",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "rtl_security",
    "verdict": "done",
    "origin": "evolution"
  },
  {
    "family": "review",
    "review": "tb_quality",
    "verdict": "done",
    "origin": "evolution"
  }
]
```
