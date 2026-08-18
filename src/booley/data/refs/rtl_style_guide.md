# RTL Style Guide

Single source of truth for coding standards. Used by review agents and RTL coding agents.

Project-specific overlays may be supplied by the caller or ticket context.

## 1. Comments

| Rule | Severity |
|------|----------|
| Comments must not contradict code — wrong comment is worse than none | MAJOR |
| Explain *why*, not *what* — no `// increment counter` above `cnt <= cnt + 1` | MINOR |
| No style-choice comments (e.g., "using wire instead of macro for safety") — only comment on functionality | MINOR |
| No stale references to removed signals, modules, or behaviors | MINOR |
| Complex logic, FSM transitions, non-obvious bit manipulation must have comments | MINOR |

## 2. Naming & Magic Numbers

| Rule | Severity |
|------|----------|
| No single-letter signals (except `i`, `j`, `k`), no generic names (`data`, `result`, `temp`) — use domain terms | MINOR |
| Consistent naming within a module — no mixing naming styles for the same kind of signal | MINOR |
| No confusable names — signal pairs that differ only by a short suffix/abbreviation (e.g. `_prot` vs `_protect`, `_en` vs `_enable`, `_sel` vs `_select`) within the same scope or port list. Rename one to make the distinction obvious (e.g. append `_mode`, `_op`, `_flag`) | MINOR |
| `parameter` and `localparam` names must be UPPER_CASE (e.g. `NBW_DATA`, `N_ROUNDS`) — lowercase constants are invisible to automated coverage filters and cause false coverage failures | MAJOR |
| No magic numbers — derive constants from existing package-level parameters | MINOR |
| No redundant localparams that duplicate package constants | MINOR |
| No ascending bit ranges `[lo:hi]` in ports or signals — use descending `[N-1:0]`. Ascending ranges cause silent data corruption under cocotb/VPI (integer conversion assumes left index is MSB) | CRITICAL |

## 3. Assertions & Cover Points

| Rule | Severity |
|------|----------|
| Implementation agents must NOT create new assertions (`ap_*`) — only modify existing ones if signal names/conditions change. New assertions are authored during testbench authoring, not by RTL implementation. Leave `// TODO: SVA` comments where coverage is needed | MAJOR |
| No `cover property` / `cover sequence` (`cp_*`) — cover points are prohibited in all stages | MAJOR |

## 4. Ifdef & Conditional Compilation

| Rule | Severity |
|------|----------|
| RTL behavior driven by `localparam`, not `ifdef` branches. `ifdef` only for gating module instantiations (synthesis EDA tools can't optimize away unused instances) | MAJOR |
| No near-identical `ifdef` paths differing only in names/widths/constants — consolidate via parameterization or runtime muxing | MINOR |

## 5. Arithmetic & Synthesis Cost

| Rule | Severity |
|------|----------|
| No `/`, `%`, or `*` with a non-constant operand unless a real divider/multiplier is intended — these infer dividers/multipliers on the critical path and cost area. When an operand is a power of two or compile-time constant, use a shift / mask / shift-add instead (`x >> k`, `x & (2**k-1)`, `(x<<k) ± x`). DSP-mapped multiply is only for deliberate multiplier datapaths (e.g. modular-mul), never index/offset/counter math | MAJOR |
