# Simulation tests

Tests are part of the [`sim` Flow](FLOW-SIM.md): this page covers declaring
them in `.booley/tests.toml`. [Test Runs, Test Variants and seeds](FLOW-SIM.md#test-runs-test-variants-and-seeds)
covers choosing them when you run `sim`.

`tests.toml` says what to verify on each sim Target: the Target's **Tests**,
how the testbench is told which Test to run, the **Test Variants** of a Test,
and how a Test Run is seeded.

```toml
[test_lists]                          # optional shared, reusable lists
smoke = ["reset", "basic"]

[sim]                                 # keyed by .core Target name
tests  = ["reset", "basic", "stress"]
select = "+test_id={index}"           # one option token; {index} (0-based) or {name}
skip   = ["stress"]                   # known-hangs to exclude from a full run

[sim_fast]
test_list = "smoke"                   # reference a shared list instead

[cpu_core]                            # a CPU core selecting firmware per test
tests  = ["hello.elf", "coremark.elf"]
select = "--meminit=ram,{name}"       # getopt argument, not a plusarg
```

**Section keys.** A section key is a bare Target name (`sim`) or a qualified
selector (`vlnv-suffix#name`, for example `["acme:uart:rx#sim"]`; quote it
because of the colons). The qualifier matches the way `--target` does: any
trailing segments of `vendor:library:name`, and a version is ignored. A Target
uses the qualified key that names its core and otherwise the bare key with its
name, whatever the order of the keys in the file. A section comes from one key
only: a Target never reads another core's qualified section. Two keys naming
one Target make the Simulation Flow refuse the run and `booley doctor` fail,
naming both keys; a key that names no Target also fails `booley doctor`.

A Target section holds only these keys. Any other key fails to load, and
`booley doctor` names it:

| Key | Meaning |
|---|---|
| `tests` | The Target's Tests, in order. |
| `test_list` | The name of a shared `[test_lists]` entry to use as `tests`. A section sets `tests` or `test_list`, never both. |
| `skip` | Tests that a run of the whole Target leaves out. |
| `select` | The argument template that tells the testbench which Test to run. |
| `env` | [Environment variables](#per-target-environment-env) for the Target's build and runs. |
| `variants` | The [Test Variants](#test-variants-variants) of the Target's Tests. |
| `seed` | The [seed argument template](#seeds-seed-default_seed). |
| `default_seed` | The Target's [Default Seed](#seeds-seed-default_seed). |

**Test names.** A Test name is not empty, is at most 512 UTF-8 bytes, is not
`default`, does not start with `-`, and contains no whitespace, control
character, `+`, `@`, `<`, `>`, quote or backslash. The same rule applies to Test Variant names. `+` and `@`
are reserved because a Test Run is written `test+variant@seed`. A valid name
can still contain characters that a shell interprets, such as `;`, `|`, `&`,
`$`, `(`, `)` or `*`: quote such a name on the command line. A name appears
at most once in a Target's `tests` list and at most once in its `skip` list.

**`select`** is **exactly one option token** (no embedded whitespace). Two
forms: a **plusarg** (`+…`), consumed by the testbench's `$value$plusargs`
runtime; or a **getopt argument** (`-…` / `--…`), forwarded verbatim to the sim
binary's own `main`: the form a CPU core needs when it selects boot software
with e.g. `--meminit=ram,<elf>`, which no plusarg can express. The template
must contain `{index}` (0-based) or `{name}`, and these are the only
substitutions. It cannot start with `++` or `+-`, which a simulator reads as a
different option. A Target without `select` uses `+test_id={index}`.

`skip` drops known-hanging or known-failing tests from a plain
`booley flow sim --target <target>` run. Naming a skipped test with
`--test <name>` still runs it, and an all-skip target fails preflight rather
than passing with zero tests. There is no per-invocation `--skip` option; pick
a different suite with repeated `--test` options or `--tests-file`.

## Test Variants (`variants`)

A **Test Variant** is one Test with a fixed list of extra run-time arguments,
extra environment variables, or both. Declare a Variant for every
configuration of a Test that you want to run: a longer iteration count, a
fault-injection mode, a verbose log. Booley has no option that passes
free-form arguments to a run, so that every run is a configuration the Project
declared.

```toml
[sim]
select = "+test_id={name}"
tests  = ["reset", "basic", "stress"]

[sim.variants.stress]
default = "short"
short = ["+iters=1000"]
soak  = ["+iters=100000"]
fault = { args = ["+err_rate=5"], env = { FAULT_MODE = "1" } }
```

- The table `[<target>.variants.<test>]` belongs to one Test, which must be in
  the Target's `tests`.
- A Variant is a list of arguments, or a table with `args`, `env`, or both.
  The list may be empty: `plain = []` is a Variant that adds nothing.
- `default` is required and names one of the declared Variants. The bare Test
  name means that Variant: `--test stress` and a run of the whole Target both
  run `stress+short`. Every other Variant runs only when it is named, as in
  `--test stress+soak`.
- Each argument is one token that starts with `+` or `-` and contains no
  whitespace. Like `select`, it cannot start with `++` or `+-`.
- Variant arguments reach the simulator command after the `select` argument.
  They are run-time inputs only: two Variants of a Test share one simulator
  build.

A Variant cannot change what the Target or Booley already decides:

- An argument cannot set the option of the `select` template, the option of
  the `seed` template, or `+verilator+seed+`.
- An argument cannot set a plusarg that the `.core` Target declares as a
  parameter. The Target owns its parameter values.
- `env` cannot set a [reserved name](#per-target-environment-env), nor a
  variable that the same Target's `env` table sets.
- On a [Cocotb Target](TARGETS.md#cocotb-targets-python-testbenches) a Variant carries
  `env` only. Arguments are refused there.

`booley doctor` reports each of these as a `tests.toml` failure that names the
Target, the Test and the Variant.

## Seeds (`seed`, `default_seed`)

A **Test Run** is one Test Variant simulated with one seed. Its name is
`test+variant@seed`, or `test@seed` for a Test without Variants: for example
`stress+soak@42` or `reset@1`. Reports, run directories and Goals use this
name.

Booley passes the seed of every Test Run itself and never relies on a
simulator's own default seed. How the seed reaches the simulation depends on
the Target:

| Target | What Booley passes |
|---|---|
| Verilator, HDL testbench | `+verilator+seed+<seed>`, and the rendered `seed` template when the Target declares one |
| Icarus, HDL testbench | the rendered `seed` template; without a template the Target is **unseeded** |
| Cocotb Target | cocotb's seed variable: `COCOTB_RANDOM_SEED` on cocotb 2.x, `RANDOM_SEED` on cocotb 1.x |

```toml
[sim]
tests = ["reset", "basic", "stress"]
seed = "+seed={seed}"        # optional: the testbench reads its own seed argument
default_seed = 7             # optional: this Target's Default Seed
```

- **`seed`** is an argument template for a testbench that reads its seed from
  an argument of its own. It is exactly one option token that starts with `+`
  or `-`, contains the plain field `{seed}` exactly once and no other field,
  and cannot start with `++` or `+-`. It cannot set the same option as
  `select`, and it cannot be `+verilator+seed+`. Booley renders the seed as a
  decimal number. A Cocotb Target cannot declare `seed`.
- **`default_seed`** is the seed of a Test Run that names none. It is an
  integer from 1 to 2147483647. Without it the **Default Seed** is `1`.
- An **unseeded** Target cannot apply a seed. Its Test Runs are written
  without `@seed`, and Booley refuses `--seed`, `test@seed`, `test@xN` and a
  `default_seed` key for it. To seed an Icarus Target, declare a `seed`
  template that the testbench reads.

On the simulator command the arguments appear in this order: the Target's
plusarg parameters, the `select` argument, the Variant's arguments, then the
seed arguments. A Target plusarg that sets the same option as `select` or the
`seed` template is left out, so that the testbench sees one value.

`booley doctor` prints one line per sim Target that says how its Test Runs are
seeded and with which Default Seed, or that they are unseeded and why. See
[Seeds and Test Variants on the command line](FLOW-SIM.md#test-runs-test-variants-and-seeds)
for `--test` and `--seed`.

## Per-Target environment (`env`)

Plenty of upstream testbenches are parameterized by an environment variable —
a cocotb module that branches on `os.getenv("FLAVOR")`, an SV testbench whose
C++ `main` reads a config var. Declare it per Target and Booley exports it into
the simulator process:

```toml
[sim_vanilla]
tests = ["run_test_001"]
env   = { FLAVOR = "vanilla" }        # NAME = "value", strings only

[sim_small]
tests = ["run_test_001"]
env   = { FLAVOR = "small", NOC_DEBUG = "1" }
```

The variables are exported in the shell that runs the build **and** the
simulation, inside the Sandbox. [Pre-Sim
Commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands) see them but
can't set them: their own exports die with their shell. Values must be quoted
strings, and names must be
shell-exportable identifiers (`[A-Za-z_][A-Za-z0-9_]*`). `--dry-run` previews
the exact `export` lines.

An `env` table, whether a Target's or a Test Variant's, cannot set a name
that Booley or the simulator integration owns. These names fail to load:
`MODULE`, `TESTCASE`, `TOPLEVEL`, `TOPLEVEL_LANG`, `RANDOM_SEED`,
`LIBPYTHON_LOC`, `PYTHONPATH`, `LD_LIBRARY_PATH`, and every name that starts
with `BOOLEY_`, `COCOTB_`, `PYGPI_` or `GPI_`.
