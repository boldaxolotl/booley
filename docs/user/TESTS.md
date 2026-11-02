# Tests

A **Test** is one named scenario your testbench can run: `reset`, `irq`,
`stress`. Booley only runs Tests that the Project registers for a sim Target
in `tests.toml`, so you, the Developer Agent and every Flow refer to the same
Test by the same name, and every pass or fail traces back to a declared
configuration. Project Setup writes `tests.toml` from your repository's existing
tests. After that you change it when you add a test, want to run a test in
another configuration (a **Test Variant**), or need control over random seeds.
How to run Tests and read their results is in [Simulation](FLOW-SIM.md).

A Target without any registered Tests still runs: Booley starts its testbench
once with no Test selected, so the testbench runs whatever it runs by default.

## Adding a Test

1. Ask the agent to add the test, for example *"Add a test for FIFO overflow
   to the `sim` Target."* The agent writes the testbench code and registers
   the Test name in `tests.toml`.
2. The agent runs `booley doctor`, which checks `tests.toml` and names the
   entry and the rule for any mistake.
3. Ask the agent to run it: *"Run `fifo_overflow` on `sim`."* A run of the
   whole Target includes it from now on.

The testbench has to know which Test to run. For an HDL testbench Booley
passes one argument per run that names the Test. By default it is
`+test_id=<n>`, where `<n>` is the Test's 0-based position in the Target's
list, and the testbench reads it with `$value$plusargs`. A testbench that
expects something else, such as a Test name or a firmware image on the
command line, gets its own `select` template. For a
[Cocotb Target](TARGETS.md#cocotb-targets-python-testbenches) the Tests are
the names of its cocotb test functions, and Booley selects them itself.

```toml
[sim]
tests  = ["reset", "basic", "stress"]
select = "+test={name}"               # pass the name instead of the position

[cpu_core]                            # a CPU core that boots one firmware image per Test
tests  = ["hello.elf", "coremark.elf"]
select = "--meminit=ram,{name}"
```

## Skipping known-bad Tests (`skip`)

`skip` lists Tests that hang or fail for a known reason. A run of the whole
Target leaves them out, and naming a skipped Test explicitly still runs it.
An all-skip target fails preflight rather than passing with zero tests.

```toml
[sim]
tests = ["reset", "basic", "stress"]
skip  = ["stress"]
```

## Test Variants (`variants`)

A **Test Variant** is one Test run with a fixed set of extra run-time
arguments, environment variables, or both: a longer iteration count, a
fault-injection mode, a verbose log. Booley has no option that passes
free-form arguments to a run, so every run is a configuration the Project
declared and anyone can rerun it by name. To try a Test in a new
configuration, ask the agent to add a Variant: *"Add a `soak` variant of
`stress` with 100000 iterations."*

```toml
[sim.variants.stress]
default = "short"
short = ["+iters=1000"]
soak  = ["+iters=100000"]
fault = { args = ["+err_rate=5"], env = { FAULT_MODE = "1" } }
```

- One of the Variants is the `default`. The plain Test name means that
  Variant: asking for `stress`, or running the whole Target, runs `stress`
  with `short`. Every other Variant runs only when you name it.
- Results name a Variant after its Test: `stress+soak` is Test `stress`,
  Variant `soak`.
- Variants change run-time inputs only, so all Variants of a Test share one
  simulator build.

A Variant cannot override what the Target or Booley already decides: the
Test-selecting argument, the seed arguments, a plusarg the `.core` Target
declares as a parameter, or a variable the Target's own
[`env`](#per-target-environment-env) sets. On a Cocotb Target a Variant
carries environment variables only. `booley doctor` names the Target, Test
and Variant of any such conflict.

## Seeds (`seed`, `default_seed`)

Booley gives every run an explicit random seed and records it, so a failure
replays exactly. You can run a Test once at the Target's **Default Seed**,
pin one seed, or run it N times with different seeds; see
[Simulation](FLOW-SIM.md#test-runs-test-variants-and-seeds) for how to ask.
One run of one Test Variant at one seed is a **Test Run**, and results name
it by Test, Variant and seed: `stress+soak@42`, or `reset@1` for a Test
without Variants.

How the seed reaches the simulation depends on the Target:

| Target | What Booley passes |
|---|---|
| Verilator, HDL testbench | `+verilator+seed+<seed>`, plus the `seed` template when the Target declares one |
| Icarus, HDL testbench | the `seed` template; without one the Target is **unseeded** |
| Cocotb Target | cocotb's seed variable: `COCOTB_RANDOM_SEED` on cocotb 2.x, `RANDOM_SEED` on 1.x |
| Any other simulator, HDL testbench | nothing; the Target is **unseeded** |

```toml
[sim]
seed = "+seed={seed}"        # the testbench also reads its seed from +seed=
default_seed = 7             # this Target's Default Seed; 1 when not set
```

- **`seed`** is for a testbench that reads its seed from an argument of its
  own. A Cocotb Target cannot declare it.
- **`default_seed`** is the seed of a run that names none: a number from 1
  to 2147483647. Set it when your existing regression uses a fixed seed
  other than 1.
- An **unseeded** Target cannot apply a seed, so Booley refuses any request
  for one, and its results carry no seed. To seed an Icarus Target, give its
  testbench a seed argument and declare the `seed` template.

`booley doctor` prints one line per sim Target saying how its Test Runs are
seeded and with which Default Seed, or that they are unseeded and why.

## Per-Target environment (`env`)

Some testbenches pick their configuration from an environment variable, such
as a cocotb module that branches on `os.getenv("FLAVOR")`. Declare the
variables per Target, and Booley sets them for both the build and the
simulation:

```toml
[sim_vanilla]
tests = ["run_test_001"]
env   = { FLAVOR = "vanilla" }

[sim_small]
tests = ["run_test_001"]
env   = { FLAVOR = "small", NOC_DEBUG = "1" }
```

Values are quoted strings. [Pre-Sim
Commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands) see these
variables but cannot set them. `--dry-run` shows the exact `export` lines.
To vary a variable per Test instead of per Target, use a
[Test Variant](#test-variants-variants).

## `tests.toml` reference

`tests.toml` lives in the Booley Directory (`.booley/` by default). Each
section is named after a sim Target; the optional `[test_lists]` table holds
named lists that several Targets can share.

```toml
[test_lists]
smoke = ["reset", "basic"]

[sim_fast]
test_list = "smoke"
```

| Key | Meaning |
|---|---|
| `tests` | The Target's Tests, in order. |
| `test_list` | Use a shared `[test_lists]` entry as `tests`. Set `tests` or `test_list`, not both. |
| `skip` | Tests a run of the whole Target leaves out. |
| `select` | The argument that tells an HDL testbench which Test to run. Default `+test_id={index}`. Not allowed on a Cocotb Target. |
| `env` | Environment variables for the Target's build and runs. |
| `variants` | The Test Variants of the Target's Tests, one `[<target>.variants.<test>]` table per Test. |
| `seed` | The seed argument template. |
| `default_seed` | The Target's Default Seed. Default `1`. |

Any other key fails to load, and `booley doctor` names it.

- **Section names.** A bare Target name (`[sim]`) applies to every core's
  Target of that name. To pick one core's Target, quote a qualified name:
  `["acme:uart:rx#sim"]`. It matches like `--target` does, and it wins over the
  bare name. Two qualified sections that name one Target, or a section that
  names no Target, fail `booley doctor`.
- **Names.** A Test or Variant name has no whitespace, `+`, `@`, `<`, `>`,
  quotes or backslashes, does not start with `-`, is not `default`, and
  appears once per list. `booley doctor` prints the full rule.
- **Arguments.** `select`, `seed` and every Variant argument are each one
  token with no spaces, starting with `+` (a plusarg) or `-` (an argument for
  the simulation binary's own `main`), and not with `++` or `+-`. `select`
  uses `{index}` (0-based position in `tests`) or `{name}`; `seed` uses
  `{seed}` exactly once and cannot be `+verilator+seed+`. `select` and `seed`
  cannot set the same option.
- **Argument order.** The simulator gets the Target's plusarg parameters, then
  `select`, the Variant's arguments, and the seed arguments. A Target plusarg
  that sets the same option as `select` or `seed` is dropped, so the
  testbench sees one value.
- **Environment.** Names are shell identifiers (`[A-Za-z_][A-Za-z0-9_]*`).
  No `env` table can set `MODULE`, `TESTCASE`, `TOPLEVEL`, `TOPLEVEL_LANG`,
  `RANDOM_SEED`, `LIBPYTHON_LOC`, `PYTHONPATH`, `LD_LIBRARY_PATH`, or a name
  starting with `BOOLEY_`, `COCOTB_`, `PYGPI_` or `GPI_`: Booley and the
  simulator integration own them.
