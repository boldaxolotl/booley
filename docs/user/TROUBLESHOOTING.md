# Troubleshooting

The few problems Booley can't explain by itself. For any other problem, read
the error first: it names the fix.

Then run `booley doctor`: it checks the host, the Project and the Sandbox and
prints a fix hint (`→`) under most problems; see
[Doctor](https://booley.dev/docs/doctor/). Easier still, type `/booley-heal` in
the Developer Agent chat. The agent runs Doctor, fixes what it can, checks the
result with `booley doctor --deep`, and hands you the steps only you can do on
the host. Do the same when Booley tells you its version changed.

If you suspect the agent planted Git configuration or hooks, read the
[known Sandbox escape warning](https://booley.dev/docs/sandbox/#known-sandbox-escape-through-shared-git-metadata)
before you run Doctor on the host: host-side Doctor can trigger it.

## Synthesis

### RTL simulates cleanly but `synth` rejects it under `slang`

The `slang` frontend is stricter than Verilator, and its error can name the
wrong thing. Known case: `$size()` on an array of interface instances
(`.S_COUNT($size(stat_if))`) fails with
`<file>:<line>: ERROR: 'stat_if' cannot be used in an expression`. Put the
count in a `localparam` and use it in both the declaration and the
instantiation. If you don't need `slang`, stay on the default `sv2v` frontend; see
[the frontends](https://booley.dev/docs/flow-synth/#rtl-frontend-sv2v-vs-slang).

### Yosys rejects transpiled Verilog after `sv2v` succeeds

`sv2v` can produce Verilog that Yosys can't parse, so the error points at a
line you never wrote. Known case: an array of interface instances inside a
generate block yields hierarchical function calls such as
`top.gen_x[0].u_router.my_pkg_MinBitWidth(2)` in port widths. Switch that
Target to the `slang` frontend, which reads interfaces natively: set
`frontend: slang` in the Target's `flow_options`, or ask the agent to run
`synth` with `--frontend slang` once.

## Still stuck

Type `/booley-feedback` in the Developer Agent chat while the problem is still
on screen. It records how to reproduce it, checks Booley's code before blaming
it, and saves a report locally; see
[When Booley itself misbehaves](https://booley.dev/docs/usage/#when-booley-itself-misbehaves).
Then [open an issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new),
which is public, or email it privately to
[boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
