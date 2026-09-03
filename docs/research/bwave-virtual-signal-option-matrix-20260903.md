# B-Wave virtual-signal option matrix — 2026-09-03

This note resolves
[`Reconcile the B-Wave virtual-signal option matrix`](https://github.com/boldaxolotl/booley/issues/282)
against [`origin/main` at `190b9904`](https://github.com/boldaxolotl/booley/tree/190b9904c49a74bd966762f3c677664949cc71b5).
It separates the public documentation contract, the live argument surface, and
observable behavior so the QA map does not count a parsed-but-ignored option as
working support.

## Result

The recoverable intended public contract is a five/five split:

- **Support `--virtual`:** `wave`, `find`, `sample`, `distance`, and `value`.
- **Reject `--virtual`:** `list`, `signal`, `diff`, `stats`, and `stuck`.

That intent is repeated by the bundled skill, public wrapper help, overview's
consumer-only list, the `ConsumerOpts` source comment, the virtual-reference
opening, and integration-test history. It is more specific than the overview's
generic “all query commands” sentence and the drifted individual pages.

The shipped implementation does not yet realize that intended matrix. Only
**`wave`, `find`, and `distance`** have behavior-backed positive support.
`sample` and `value` are intended-positive but parser-only: they accept and then
ignore the definition. Conversely, `signal` and `diff` are intended-negative
but mistakenly advertise and parse the option before ignoring it. `list`,
`stats`, and `stuck` already reject it correctly.

That produces the following QA matrix for the ten public query/introspection
commands:

| Command | Intended QA contract | Individual public page / live `--help` | Behavior on `190b9904` | Baseline status |
| --- | --- | --- | --- | --- |
| `list` | Reject | Omits / rejects | Clap rejects `--virtual`, exit 2 | **Matches contract** |
| `signal` | Reject | Advertises / accepts | Virtual-only selection exits 2; mixed selection silently drops the virtual; malformed definitions are ignored with exit 0 | **Parser/docs leak** |
| `wave` | Support | Advertises / accepts | Renders the virtual row, including a virtual-only selection; malformed definitions exit 2 | **Matches contract** (text output) |
| `value` | Support | Advertises / accepts | Virtual-only selection exits 2; mixed selection silently drops the virtual; malformed definitions are ignored with exit 0 | **Missing implementation** |
| `find` | Support | Advertises / accepts | Resolves the virtual as the search target in sync/async and text/JSON modes; malformed definitions exit 2 | **Matches contract** |
| `sample` | Support | Advertises / accepts | A virtual trigger exits 2; a virtual capture row is silently absent; malformed definitions are ignored with exit 0 | **Missing implementation** |
| `diff` | Reject | Advertises / accepts | Virtual-only selection exits 2; mixed selection silently drops the virtual; malformed definitions are ignored with exit 0 | **Parser/docs leak** |
| `distance` | Support | Advertises / accepts | Resolves virtuals as either event, with level and edge matching; malformed definitions exit 2 | **Matches contract** (text output) |
| `stats` | Reject | Explicitly rejects / rejects | Clap rejects `--virtual`, exit 2 | **Matches contract**; hidden library support is not public |
| `stuck` | Reject | Explicitly rejects / rejects | Clap rejects `--virtual`, exit 2 | **Matches contract** |

For QA, the intended contract supplies the expected result; the baseline status
says whether current `main` passes it. Merely appearing in help or reaching
`ExtractConfig.virtual_defs` does not establish support. The four drift entries
must remain visible known failures until fixed: implement the two missing
positive consumers and remove the option from the two negative parsers/pages.

`build` is outside the query/introspection matrix and rejects `--virtual` at
argument parsing. `gui` is the separate human-facing wrapper command and its
public page exposes only real trace signals, not virtual definitions
([GUI page](../../crates/bwave/docs/public/commands/gui.md#L1-L18),
[signal selection](../../crates/bwave/docs/public/commands/gui.md#L48-L58)).
The meta commands `schema`, `docs`, and `skill` likewise have no trace-query
option surface.

## Public documentation says three different things

The canonical vocabulary establishes B-Wave as the agent-facing query surface
and a Virtual Signal as a named predicate evaluated over waveform signals
([`docs/CONTEXT.md`](../CONTEXT.md#L252-L268)). The broader public user guide
only promises actual-value queries, triggered sampling, and traces; it does not
define an option matrix
([`docs/user/FEATURES.md`](../user/FEATURES.md#L113-L117)). The detailed bundled
corpus is therefore where the command-level promise lives.

Within that corpus:

- The overview says **all seven query commands** accept `--virtual`
  ([query table](../../crates/bwave/docs/public/commands/overview.md#L17-L30)),
  then says the consumer-only option is available on only five — `wave`,
  `find`, `sample`, `distance`, and `value` — omitting `signal` and `diff`
  ([consumer options](../../crates/bwave/docs/public/commands/overview.md#L63-L70)).
- The virtual-signal reference first describes the same five commands
  ([opening](../../crates/bwave/docs/public/reference/virtual-signals.md#L3-L12)),
  then adds `signal` to its accepted set while explicitly placing `diff` in the
  rejected set
  ([scope table](../../crates/bwave/docs/public/reference/virtual-signals.md#L14-L21)).
- Every individual query page advertises `--virtual`: `signal`
  ([synopsis and option](../../crates/bwave/docs/public/commands/signal.md#L5-L38)),
  `wave` ([synopsis](../../crates/bwave/docs/public/commands/wave.md#L5-L9)),
  `value` ([synopsis](../../crates/bwave/docs/public/commands/value.md#L5-L9)),
  `find` ([synopsis](../../crates/bwave/docs/public/commands/find.md#L5-L11)),
  `sample` ([synopsis and option](../../crates/bwave/docs/public/commands/sample.md#L5-L38)),
  `diff` ([synopsis and option](../../crates/bwave/docs/public/commands/diff.md#L5-L34)),
  and `distance`
  ([synopsis and option](../../crates/bwave/docs/public/commands/distance.md#L5-L46)).
  The strongest conflicting claim is `diff`'s executable-looking example
  ([`diff` example](../../crates/bwave/docs/public/commands/diff.md#L89-L95)),
  while `sample` similarly promises a virtual trigger
  ([`sample` example](../../crates/bwave/docs/public/commands/sample.md#L95-L101)).
- The individual `stats` and `stuck` pages explicitly reject the option
  ([`stats`](../../crates/bwave/docs/public/commands/stats.md#L21-L22),
  [`stuck`](../../crates/bwave/docs/public/commands/stuck.md#L22-L26)); `list`
  presents structural names and types only
  ([`list`](../../crates/bwave/docs/public/commands/list.md#L9-L14)).
- The bundled agent skill narrows the option back to the five-command set and
  omits `signal` and `diff`
  ([`crates/bwave/docs/skills/bwave.md`](../../crates/bwave/docs/skills/bwave.md#L31-L35)).
  The public Python wrapper help repeats exactly that five-command statement
  ([`src/booley/bwave/cli.py`](../../src/booley/bwave/cli.py#L1231-L1241)), while
  also telling users that per-command live help is authoritative
  ([wrapper help](../../src/booley/bwave/cli.py#L1158-L1162)).
- The source comment on the option group names the same five commands
  ([`main.rs`](../../crates/bwave/src/main.rs#L155-L168)), and test history says
  `stats` was deliberately removed because virtuals are scoped to those five
  consumer commands
  ([`integration_test.rs`](../../crates/bwave/tests/integration_test.rs#L1905-L1916)).

Thus the individual pages and live help agree on seven parser-visible commands,
but neither agrees with the actual three-command implementation. The repeated,
more-specific five-command statements establish the intended contract: the
extra `signal`/`diff` flags are leaks, while missing `sample`/`value` behavior is
an implementation gap. The reference is specifically wrong about current
`diff` parsing (it says rejected) and right about the intended boundary.

## Parser wiring versus consumers

The shared Clap `ConsumerOpts` owns `--virtual`, although its source comment
names only the same five-command subset as the wrapper help
([`main.rs`](../../crates/bwave/src/main.rs#L155-L168)). The argument struct is
actually flattened into all seven query commands: `signal`, `wave`, and `value`
([`main.rs`](../../crates/bwave/src/main.rs#L249-L319)); `find`, `sample`, and
`diff` ([`main.rs`](../../crates/bwave/src/main.rs#L321-L428)); and `distance`
([`main.rs`](../../crates/bwave/src/main.rs#L430-L463)). Each handler copies the
parsed definitions into `ExtractConfig`, including `diff`
([handler wiring](../../crates/bwave/src/main.rs#L918-L987),
[`find`/`sample`](../../crates/bwave/src/main.rs#L990-L1128),
[`diff`/`distance`](../../crates/bwave/src/main.rs#L1131-L1185)). That is why
live help accepts seven commands.

Only three cache consumers build and use the definitions:

- `find` builds virtual entries before its empty-match gate and walks matching
  virtual transitions in both edge and level modes
  ([build/match](../../crates/bwave/src/cache.rs#L1231-L1265),
  [virtual walk](../../crates/bwave/src/cache.rs#L1463-L1528)).
- `distance` builds the entries and passes them to both event collectors
  ([distance setup](../../crates/bwave/src/cache.rs#L2363-L2391)); the collector
  explicitly combines real and virtual pattern matches
  ([event collection](../../crates/bwave/src/cache.rs#L2215-L2246),
  [virtual events](../../crates/bwave/src/cache.rs#L2305-L2359)).
- `wave` builds virtuals before its empty-match gate and appends a rendered row
  for each definition
  ([setup](../../crates/bwave/src/cache.rs#L3261-L3269),
  [rows](../../crates/bwave/src/cache.rs#L3387-L3401)).

The other four never call the common virtual builder:

- `signal` says directly that it renders no virtual rows, then matches and
  walks only store signals
  ([`trace_from_cache`](../../crates/bwave/src/cache.rs#L2564-L2581)).
- `value` gates on real `match_signals` and creates output values only from
  those real indices
  ([`snapshot_from_cache`](../../crates/bwave/src/cache.rs#L1882-L1918),
  [value collection](../../crates/bwave/src/cache.rs#L2002-L2052)).
- `sample` resolves the trigger only against real cache signals and samples
  only real watched indices
  ([trigger resolution](../../crates/bwave/src/cache.rs#L1635-L1669),
  [sample rows](../../crates/bwave/src/cache.rs#L1774-L1797),
  [render loop](../../crates/bwave/src/cache.rs#L1845-L1865)).
- `diff` gates, reads, compares, and renders only real matched indices
  ([`diff_from_cache`](../../crates/bwave/src/cache.rs#L2107-L2186)).

This also explains why malformed definitions are rejected only on the three
working commands: parse and resolution happen inside `build_virtuals`, which
exits 2 on either failure
([`build_virtuals`](../../crates/bwave/src/cache.rs#L522-L560)). The four
parser-only commands never call it, so even `--virtual "bad = "` succeeds and
is ignored.

### Hidden `stats` capability is not public support

`ExtractConfig` exposes `virtual_defs` internally
([`lib.rs`](../../crates/bwave/src/lib.rs#L43-L50)), and
`stats_from_cache` builds virtuals, counts them, and emits virtual statistics
([setup](../../crates/bwave/src/cache.rs#L721-L755),
[virtual stats](../../crates/bwave/src/cache.rs#L883-L915)). But `StatsArgs` has
no `ConsumerOpts`
([`main.rs`](../../crates/bwave/src/main.rs#L465-L479)), so no public native or
wrapper invocation can populate that field. The integration tests explicitly
record that CLI virtual-stat tests were removed
([`integration_test.rs`](../../crates/bwave/tests/integration_test.rs#L1905-L1916)).
QA must treat this as hidden/dead CLI-inaccessible implementation, not infer
support from the function body.

## Generated schema does not define option availability

`bwave schema` prints the JSON document embedded from
`schema/bwave.json`
([`run_schema`](../../crates/bwave/src/main.rs#L1251-L1258)). At this revision,
the generated stdout is byte-for-byte identical to that file (SHA-256
`6418e2c68d9f8e4bebdba4a7ea64aa0bc6da9a70c961144e7c4d4e50768cb9e0`).
It is an **output-envelope schema**, not a command-input or option schema: its
only command enum is `list`, `value`, `find`, and `stats`
([schema header and enum](../../crates/bwave/schema/bwave.json#L1-L17)), matching
the documented JSON-output boundary. It therefore cannot establish that a
command accepts or consumes `--virtual`.

Of the behavior-backed virtual commands, only `find` has schema-backed JSON.
A virtual-only `find --format json` produced a valid `find` envelope and a
virtual match. `wave` and `distance` remain text-only; their individual pages
say JSON is not implemented
([`wave`](../../crates/bwave/docs/public/commands/wave.md#L61-L62),
[`distance`](../../crates/bwave/docs/public/commands/distance.md#L83-L84)).
This JSON boundary is coherent and separate from the virtual-option conflict.

## Reproducible command evidence

All probes used the native debug binary built from the pinned checkout; it
reported `bwave 0.2.11`. The deterministic input was the three-signal
`test_basic.vcd` fixture, whose `data[7:0]` increments once per cycle after
reset ([fixture](../../crates/bwave/tests/fixtures/test_basic.vcd#L1-L71)):

```bash
cargo build --manifest-path crates/bwave/Cargo.toml
./crates/bwave/target/debug/bwave build \
  crates/bwave/tests/fixtures/test_basic.vcd \
  -o /tmp/bwave-virtual-matrix.fst
```

The three positive probes produced values tied to that known sequence:

```text
$ bwave wave /tmp/bwave-virtual-matrix.fst -s hi -t 4:9 \
    --with-reset --virtual "hi = *data > 'd5"
cycle  4  5  6  7  8  9
   hi  0  0  0  0  1  1
# exit 0

$ bwave find /tmp/bwave-virtual-matrix.fst hi 'h1 --first --format json \
    --with-reset --virtual "hi = *data > 'd5"
{"command":"find","data":{"count":1,"matches":[{"time":8,"name":"hi","value":"1"}],...},...}
# exit 0

$ bwave distance /tmp/bwave-virtual-matrix.fst odd rising --with-reset \
    --virtual "odd = *data[0]"
@ 2 -> @ 4  d=2
...
@ 16 -> @ 18  d=2
# 8 pairs, unit: cycles
# exit 0
```

The four parser-only probes establish the failure modes, rather than relying on
source inference:

```text
$ bwave signal ... -s data -s hi --virtual "hi = *data > 'd5"
# warns that hi matched nothing, prints only data, exit 0

$ bwave value ... --at 9 -s data -s hi --virtual "hi = *data > 'd5"
# warns that hi matched nothing, prints only data, exit 0

$ bwave sample ... hi rising -s data --virtual "hi = *data > 'd5"
ERROR: --sample: no signals match trigger pattern 'hi'
# exit 2

$ bwave sample ... data rising -s hi --virtual "hi = *data > 'd5"
# reports a trigger but emits no captured row, exit 0

$ bwave diff ... 5 10 -s data -s hi --virtual "hi = *data > 'd5"
# warns that hi matched nothing, compares only data, exit 0
```

Repeating `signal`, `value`, `sample`, and `diff` with `--virtual "bad = "`
still exited 0 and printed ordinary real-signal output. Repeating `wave`,
`find`, and `distance` with that malformed definition printed
`ERROR: --virtual ... virtual signal expression cannot be empty` and exited 2.

Finally, live `--help` exposed `--virtual <DEF>` on exactly `signal`, `wave`,
`value`, `find`, `sample`, `diff`, and `distance`; `list`, `stats`, and `stuck`
did not list it, and an actual invocation of each rejected it as an unexpected
argument with exit 2. This agrees with the Clap struct wiring, but not with the
three-command behavior-backed support set.

## QA consequences

The scenario suite can now freeze these assertions without ambiguity:

1. Positive virtual coverage belongs on `wave`, `find`, `sample`, `distance`,
   and `value`. Exercise a virtual-only row/target, bad-definition exit 2,
   `find` JSON, virtual-triggered sampling, a virtual snapshot, and both
   same-signal and two-event `distance` across the suite. On the current
   baseline, the `sample` and `value` assertions are known failures.
2. Rejection coverage belongs on `list`, `signal`, `diff`, `stats`, and
   `stuck`, asserting argument-parser exit 2. On the current baseline, `signal`
   and `diff` are known failures because the parser accepts and silently ignores
   the option. Do not invoke hidden virtual stats through a library seam.
3. Keep the silent-success probes for all four drift commands until the fixes
   land. A plausible partial real-signal result is more dangerous than a clean
   virtual-only error because it can be mistaken for the requested result.

The implementation/documentation follow-up is now mechanical rather than an
open product choice: add virtual consumption and bad-definition validation to
`sample` and `value`; remove `ConsumerOpts` from `signal` and `diff`; and make
the overview, reference, individual command pages, wrapper help, bundled skill,
live help, and behavior tests state the same five/five matrix.
