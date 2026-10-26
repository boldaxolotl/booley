# Lint (`lint`)

`lint` runs the linter chosen by each Target: Verilator for structural and
semantic checks, Verible for style and naming. To run both, declare two Targets
and select both.

```bash
booley flow lint --target lint_soc,lint_style --scope rtl/fifo.sv
```

For Target recipes, see [Verilator lint Target](TARGETS.md#verilator-lint-target)
(`.vlt` waivers with `file_type: vlt`) and
[Verible lint Target](TARGETS.md#verible-lint-target) (style rules and waivers).
[Target authoring](TARGETS.md#target-authoring) covers EDA-tool compatibility.

- `--scope <file,...>` limits reported findings to the listed files.
- `[flows.lint].warnings_as_errors` decides whether warnings fail the exit code.
  Either way the report keeps the real counts, and `LINT:{target}` is only
  satisfied with zero findings.

The report lists deduplicated findings by file, line, rule, and message, and
points to the full log.

## Configuration (`[flows.lint]`)

Beyond the shared `enabled` and positive-integer `timeout_ms` settings, lint
takes an optional `warnings_as_errors` (default `true`):

```toml
[flows.lint]
timeout_ms = 120000
warnings_as_errors = false
```

Set `false` to keep warnings in the console/report but exit 0 on a
warnings-only run, so a CI gate only fails on hard errors.
