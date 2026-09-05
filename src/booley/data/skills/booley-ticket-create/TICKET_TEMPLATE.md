---
# -- User-provided fields (set at creation time) ----------------------
summary: <one-line description, becomes branch name slug>
type: feature | bugfix | refactor | verification
branch: <development branch to branch from and merge into>
scope:                           # files in scope (Booley commits only listed paths)
  - <rtl/file1.sv>               # existing file — must exist at creation time
  - <tb/file1_tb.sv>             # glob patterns supported: rtl/*.sv, tb/*.sv
  - <rtl/new_file.sv [new]>      # [new] tag = file will be created (must NOT exist yet)
spec: ""                         # optional path to architecture spec
on_success:
  destination: review               # review | done
  merge: true                       # merge feature branch on completion
  cleanup: true                     # after merge, delete worktree/branch on completion
  triage_report: true               # prepare rich HTML explanation before review
  remove_targets: []                # criterion-bound Targets deleted only from accepted merge
dependencies: []
priority: medium

# -- Acceptance criteria ------------------------------------------------
# STRUCTURE only — `booley cheat --criteria` is the single source of truth for criterion names,
# params, and the `targets:` scoping key (rendered from criteria.toml + the MCP tool
# registry); take the vocabulary from there. Each criterion maps to the Booley Flow that
# satisfies it. Per-config criteria expand automatically: lint_clean: [cfg1, cfg2] ->
# lint_clean_cfg1, lint_clean_cfg2. RTL criteria (lint/synthesis/review_rtl) auto-reset
# on RTL changes; TB criteria (sim/review_tb) auto-reset on TB changes.
criteria:
  mandatory:
    # --- Mechanical (per-config) -------------------------------------------
    lint_clean: [config1, config2]           # -> lint Flow; expands per config
    sim_pass:                                # -> sim Flow
      - tb/file1_tb.sv @ config1 @ all @ pass -> pass   # tb @ config @ test @ current -> expected
      - tb/file1_tb.sv @ config1 @ smoke @ fail -> pass  # single named test
      - tb/file2_tb.sv @ config2 @ all @ pass -> pass    # multiple TBs supported
    cycle_count:                             # -> sim Flow; per Target + named test
      - target: sim_coremark
        test: coremark
        cycle_count_max: 100000              # absolute inclusive cap
        cycle_count_reduce_at_least: 5%      # ≥5% reduction vs Acceptance Basis
        cycle_count_reduce_at_least_cycles: 2000  # ≥2000-cycle reduction vs basis
    synthesis_ok:                            # -> synth Flow
      targets:                               # strings use one Target at both revisions
        - target1
        - {baseline: target2_before, candidate: target2_after}  # relative thresholds only
      cell_count_max: 500                    # absolute cap (optional)
      cell_count_reduce_at_least: 10%        # require ≥10% reduction vs basis (optional)
    fpga_impl_ok:                            # -> fpga Flow
      targets: [target1, target2]            # paired mapping form is also supported
      lut_count_max: 100000                  # FPGA LUT budget (optional)
      ff_count_max: 100000                   # FPGA flip-flop budget (optional)

    # --- Review ------------------------------------------------------------
    # Each review focus is a separate criterion. Bind TB review explicitly
    # when more than one structured sim_pass Target is present.
    review_rtl_bugs: true              # -> corrective _clean review
    review_tb_quality: true            # -> corrective _clean review; unique sim owner derived
    # review_tb_quality: {target: target1}  # multi-Target ticket: bind explicitly

  optional:
    # --- More review focuses (opt-in per ticket) ---------------------------
    review_rtl_spec: true          # -> corrective reviewer --category rtl --focus spec
    review_rtl_protocol: true      # -> corrective reviewer --category rtl --focus protocol
    review_rtl_security: true      # -> corrective reviewer --category rtl --focus security
    review_rtl_optimization: true  # -> corrective reviewer --category rtl --focus optimization
    review_rtl_code_style: true    # -> corrective reviewer --category rtl --focus code_style

    # --- Mutation (per Target; all runnable Target tests are implicit) -----
    # SVA is authored inline as part of TB authoring — no separate criterion.
    mutation_score:
      - target: sim_default
        scope: [rtl/design.sv]
        min_detected: 8
        total: 10

# -- Runtime fields are stamped by Booley or stored in logs/<slug>/.runtime/progress.json --
# feature_branch, created
# integration_base is unsupported; Acceptance Basis participants name destination refs.
# current_tool, tools_completed, last_update, blocked_reason, blocked_tool
---

## Description

Structure depends on ticket type. For a greenfield module the `spec` field should point at
the architecture spec.

### For `feature`

```markdown
## Description

### Current State
<What the module currently does, relevant interfaces and behavior. For greenfield: "N/A — new module">

### Required Changes
<What needs to change and why — reference spec or issue if applicable>

### Affected Interfaces
<Which ports, signals, or protocols are impacted by the change>
```

### For `bugfix`

```markdown
## Description

### Failing Simulation
<Config/test that fails — the agent uses this as its starting point>

### Observed Symptoms
<What you see: error messages, signal mismatches, assertion failures, unexpected waveform behavior>

### Known-Good Reference (if known)
<Commit SHA or config where the failing test passes — lets the developer git-diff to isolate
the regression. Leave blank if unknown or if it never passed>

### Suspected Root Cause (if known)
<Best guess — helps the developer focus its investigation. Leave blank if unknown>
```

### For `refactor`

```markdown
## Description

### Current Structure
<How the code is organized now and why it's problematic>

### Target Structure
<How it should be organized after refactoring>

### Invariants
<What must NOT change — behavior, interfaces, timing, area>
```

### For `verification`

```markdown
## Description

### Coverage Gaps
<What behaviors, states, or scenarios are untested>

### Verification Strategy
<Stimulus and checking approach — constrained random, directed, assertions, scoreboard>

### RTL Boundary
<RTL must NOT be modified — list modules or signals the TB targets>
```

### Implementation Plan (detailed-plan tickets only)

Detailed-plan tickets append this section after `## Description`; it carries the plan
distilled from the design-grilling session (how to write it: `grilling.md`). Lightweight
tickets omit it and rely on the developer's inline planning.

```markdown
## Implementation Plan

### Approach
<Chosen design and key decisions (note rejected alternatives where relevant)>

### Implementation Steps
<Ordered, file-by-file breakdown — the sequence a coder would follow>

### Interface Changes
<New/changed ports, signals, widths, handshakes, config ifdefs>

### Edge Cases & Risks
<Corner cases, reset/CDC, hidden breakage — and how each is handled>

### Verification
<How the change is proven: TBs/tests, new scenarios, definition of done>

### Open Questions
<Anything unresolved after grilling — omit if none>
```

---

<!-- Acceptance criteria live in the frontmatter `criteria:` section.
     The developer agent reads these and selects Booley Flows to satisfy each criterion. -->
