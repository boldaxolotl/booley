### Ticket Create invocations

The scenario itself is the source of both Tickets. Do not ask Ticket Create to infer missing semantics. Pass the complete field sets below in one creation phase:

- Codex form: `$booley-ticket-create --agent --no-confirm <complete structured scenario payload>`
- Claude form: `/booley-ticket-create --agent --no-confirm <complete structured scenario payload>`

These are skill invocations, not ordinary CLI commands. Enqueue automatically publishes the immutable Acceptance Basis; there is no manual seal, Target Contract, `base_sha`, or second confirmation. Ticket creation may author only the approved Target definitions, owned `tests.toml` tables, and empty `[new]` placeholders. The Developer Agent authors the implementation.

Both Tickets use:

```yaml
on_success:
  destination: done
  merge: true
  cleanup: true
  triage_report: true
priority: medium
```

Only one automatic retry is permitted, with `max_attempts: 1`, and only when the exact recognized error is `API Error: Response stalled mid-stream`. Ordinary crashes, test failures, timeouts, context exhaustion, and usage-limit failures are not retried.

### Ticket 2 — `rv32-zbb-pcpi`

Type: `feature`. Dependency: `dhrystone-self-checking-cycle-contract`. Technical authority: `/opt/riscv-docs/riscv-isa-manual.html`.

Scope:

```yaml
- picorv32.v
- testbench.v
- testbench_wb.v
- Makefile
- tests/zbb.S [new]
```

Required implementation:

- Implement all 18 agreed RV32 Zbb operations: `ANDN`, `ORN`, `XNOR`, `CLZ`, `CTZ`, `CPOP`, `MIN`, `MINU`, `MAX`, `MAXU`, `SEXT.B`, `SEXT.H`, `ZEXT.H`, `ROL`, `ROR`, `RORI`, `ORC.B`, and `REV8`.
- Add `ENABLE_ZBB`, defaulting to `0`, across core, AXI, and Wishbone wrappers.
- Use an internal registered PCPI implementation with a fixed one-cycle response.
- Test enabled execution through all three wrappers.
- In the disabled test, arm a distinct MMIO marker immediately before the first Zbb encoding and require the ensuing illegal-instruction trap. No unrelated trap may count as success.
- Preserve passing behavior on the existing default main/core, AXI, Wishbone, lint, and physical-synthesis Targets.
- Consume and genuinely execute the refreshed persistent `sim_dhry_checked` Target from Ticket 1.

Ticket creation authors seven candidate Targets and their owned test tables, all ephemeral:

```yaml
target_plan:
  - {target: sim_core_zbb, role: ephemeral}
  - {target: sim_axi_zbb, role: ephemeral}
  - {target: sim_wb_zbb, role: ephemeral}
  - {target: sim_zbb_disabled, role: ephemeral}
  - {target: lint_core_zbb, role: ephemeral}
  - {target: synth_core_zbb, role: ephemeral}
  - {target: fpga_core_zbb, role: ephemeral}
```

They remain available through execution and acceptance, then their definitions and unambiguously owned test tables are removed automatically. Do not use the retired `on_success.remove_targets` mechanism.

Mandatory Criteria exercise all remaining catalog families:

- `elab_pass` for `sim_core_zbb`, `sim_axi_zbb`, `sim_wb_zbb`, and `sim_zbb_disabled`.
- `elaborate_standalone: true`.
- `lint_clean: [lint_core_zbb]`.
- `sim_pass` for existing main/core, AXI, and Wishbone regressions (`pass -> pass`); all four new Zbb simulation Targets (`fail -> pass`); and refreshed `sim_dhry_checked` (`pass -> pass`).
- `mutation_score` on `picorv32.v` through `sim_core_zbb`, with `min_detected: 14` and `total: 15`. Mutations should target the new Zbb decode, result generation, PCPI handshake, and enable gating.
- `synthesis_ok` using the directed pair `{baseline: synth_core, candidate: synth_core_zbb}`, with `cell_count_increase_at_most: 11%` and `critical_path_ps_increase_at_most: 3%`. These exact limits were measured with Zbb enabled and must not be relaxed.
- `fpga_impl_ok` for `fpga_core_zbb`, requiring successful completion and a fresh artifact but no LUT threshold. It is mandatory only where the exact Vivado profile is runnable.
- Advisory completion reviews `review_rtl_bugs_done`, `review_rtl_protocol_done`, `review_rtl_spec_done`, `review_rtl_code_style_done`, `review_rtl_optimization_done`, `review_rtl_security_done`, and a Target-bound `review_tb_quality_done`.
- In addition, corrective `review_rtl_bugs_clean` is mandatory, so at least one review family exercises the clean disposition.

Across the two Tickets, the suite must exercise all 15 agreed Criterion families: elaboration, standalone elaboration, lint, six RTL review focuses, TB-quality review, simulation, cycle count, mutation, synthesis, and FPGA implementation.

### Enabled AXI Target

Add seventh ephemeral Target `sim_axi_zbb` to Ticket2 Target Plan. The [pinned `testbench.v`](https://github.com/YosysHQ/picorv32/blob/a473fc8fca393771d83b0ffcf0b14db3393339d8/testbench.v#L11) declares top `testbench`, instantiates `picorv32_wrapper`, and that wrapper instantiates `picorv32_axi` (line 163). Bind `sim_axi_zbb` to this existing AXI testbench top, approved Zbb firmware/test source, and `ENABLE_ZBB=1` propagated through the already-authorized `testbench.v` scope. Preserve the original existing-target AXI regression separately. Require `elab_pass` and explicit Zbb `fail -> pass` simulation for this Target. It follows the same ephemeral retention/removal rules as the original six Targets, so all seven remain until acceptance and are removed afterwards with their unambiguously owned test tables.

