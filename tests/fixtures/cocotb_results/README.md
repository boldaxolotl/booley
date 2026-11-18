# Cocotb result compatibility sample

`2.1.0/results.xml` is one complete producer-emitted document containing a
passing test, an assertion failure, and an explicitly skipped test. It replaces
the authored mixed-suite input in the result parser tests. Ordinary pytest runs
read the XML and require neither Cocotb nor a simulator.

## Provenance

Captured on 08 SEP 2026 from the included `capture.sv`, `capture_cases.py`, and
`Makefile`, through Cocotb's normal Icarus regression (not its XML writer API):

- Cocotb **2.1.0**, verified with `cocotb-config --version` and the runtime log.
- Icarus Verilog **13.0 (stable)**, version output `dfeee90-dirty`.
- Embedded Python **3.13.8**, as reported by the simulator log.
- Local Sandbox Image `booley-sandbox:latest`, image ID
  `sha256:47a55d8d1ec8c15d73da106972df2b6f0bf0025b2a12d1606336069a0d7360a4`.
- Seed `1` and hostname `cocotb-results` were fixed at generation time.

From the repository root, with that image available:

```sh
capture_dir=$(mktemp -d)
docker run --rm --network none --hostname cocotb-results \
  --user "$(id -u):$(id -g)" --entrypoint sh \
  --mount type=bind,src="$PWD/tests/fixtures/cocotb_results",dst=/source,readonly \
  --mount type=bind,src="$capture_dir",dst=/capture \
  booley-sandbox:latest -c '
    cp /source/capture_cases.py /source/capture.sv /source/Makefile /capture/ &&
    cd /capture &&
    test "$(cocotb-config --version)" = 2.1.0 &&
    COCOTB_RANDOM_SEED=1 make'
```

The observed `make` exit code is **2**, because the intentional assertion fails.
Verify the log reports `TESTS=3 PASS=1 FAIL=1 SKIP=1` and inspect
`$capture_dir/results.xml`; an unsuccessful command alone is not evidence of a
valid capture. The three cases are `test_reset` (constant signal passes after
30 ns), `test_fail` (intentional assertion after another 30 ns), and
`test_skipped` (`@cocotb.test(skip=True)`).

## Sanitization

Only these value substitutions were applied, directly to the emitted text:

| Field | Captured value | Checked-in value |
|---|---|---|
| Suite `timestamp` | `2026-09-08T07:14:18.805617+00:00` | `2000-01-01T00:00:00.000000+00:00` |
| Absolute Python paths in file properties and traceback | `/capture/capture_cases.py` | `capture_cases.py` |
| Passing case `sim_time_ratio` property | `229219.35262496016` | `0.0` |
| Failing case `sim_time_ratio` property | `388480.2557929054` | `0.0` |

All emitted suite/testcase `time` values were already `0.000`; no substitution
was needed. Future captures can have different wall-clock times and ratios.
Simulation times, including `29.999999999999996`, are retained exactly.
No XML reserialization, formatting, attribute renaming, namespace changes, or
diagnostic rewriting was performed. The producer emits no XML namespace.

Unlike the former 2.0.1-shaped literal, this output carries per-case properties,
suite counts/timestamp/hostname, `failure` message/type attributes plus traceback
text, `system-err`, and a skipped message. The parser must preserve the assertion
type and message without substituting the traceback or seed-only diagnostic.
Booley reconciles the selected raw `skipped` case to **INCONCLUSIVE**.

This corpus protects only result-document compatibility associated with
`5a5239fb` and focused diagnostics associated with `6ee1e78a`. It makes no claim
about VPI/environment/runtime behavior, console-based timeout recovery, trace
artifacts, or Coverage Record (#213).

## Zero-match sample

`2.1.0/results-no-match.xml` is what Cocotb 2.1.0 writes when its test filter
matches no test: the document holds no `<testcase>`, because 2.1 omits excluded
tests instead of listing them as `skipped` (2.0.1 listed them). It backs only
the empty-results reconciliation of a focused selection.

Captured on 18 NOV 2026 from `no_match_cases.py` (one `@cocotb.test()` with
`@cocotb.parametrize(width=[1, 2])`, so its rendered IDs are
`test_param/width=1` and `test_param/width=2`), `capture.sv`, and `Makefile`:

- Cocotb **2.1.0**, verified with `cocotb-config --version`.
- Local Sandbox Image `booley-sandbox:latest`, image ID
  `sha256:434acae01dbc78416c762555f08842e3e96e547e86eaa2ed3b1682ce56d1ad0b`.
- Exit code 0 and the console warning `No tests left after filtering with:
  ^no_match_cases\.(test_param)$`, the filter form Booley builds for a
  selected `test_param`, which is not a rendered ID.

```sh
capture_dir=$(mktemp -d)
docker run --rm --network none --hostname cocotb-results \
  --user "$(id -u):$(id -g)" --entrypoint sh \
  --mount type=bind,src="$PWD/tests/fixtures/cocotb_results",dst=/source,readonly \
  --mount type=bind,src="$capture_dir",dst=/capture \
  booley-sandbox:latest -c '
    cp /source/no_match_cases.py /source/capture.sv /source/Makefile /capture/ &&
    cd /capture &&
    test "$(cocotb-config --version)" = 2.1.0 &&
    COCOTB_RANDOM_SEED=1 make COCOTB_TEST_MODULES=no_match_cases \
      "COCOTB_TEST_FILTER='\''^no_match_cases\.(test_param)\$\$'\''"'
```

No substitutions were applied: the file is the emitted bytes (two lines, no
trailing newline; no timestamp, path, or hostname appears in it).

## Skip samples

`2.1.0/results-skip-unfiltered.xml` and `2.1.0/results-skip-filtered.xml`, with
their run logs `2.1.0/skip-unfiltered.log` and `2.1.0/skip-filtered.log`, show
how Cocotb 2.1.0 reports a skip. Every `<testcase>` carries the same fixed
`<skipped message="Test was skipped" />`; the cause and the user's message
appear only in the log line `skipping <module>.<name>`.

Captured on 18 NOV 2026 from `skip_cases.py`, `capture.sv`, and `Makefile`:

- `test_runtime_skip`: `pytest.skip("feature not present in this
  configuration")`, logged as
  `skipping skip_cases.test_runtime_skip: <message>`.
- `test_runtime_skip_bare`: `pytest.skip()`, logged with an empty message.
- `test_runtime_skip_multiline`: a message of three lines (the first empty);
  Cocotb writes the later lines with no prefix, indented to the column where
  `skipping` starts.
- `test_decorator_skip`: `@cocotb.test(skip=True)`, logged as
  `skipping skip_cases.test_decorator_skip (4/4)`. Only the unfiltered run
  skips it: with `COCOTB_TEST_FILTER` set Cocotb runs a selected decorated test.

The unfiltered run is Cocotb's regression mode (`TESTS=4 PASS=0 FAIL=0 SKIP=4`);
the filtered run uses the filter Booley builds for the three runtime-skip
tests (`TESTS=3 PASS=0 FAIL=0 SKIP=3`), shown in the command below. Both exit 0.

- Cocotb **2.1.0**, verified with `cocotb-config --version`.
- Local Sandbox Image `booley-sandbox:latest`, image ID
  `sha256:434acae01dbc78416c762555f08842e3e96e547e86eaa2ed3b1682ce56d1ad0b`.

```sh
capture_dir=$(mktemp -d)
docker run --rm --network none --hostname cocotb-results \
  --user "$(id -u):$(id -g)" --entrypoint sh \
  --mount type=bind,src="$PWD/tests/fixtures/cocotb_results",dst=/source,readonly \
  --mount type=bind,src="$capture_dir",dst=/capture \
  booley-sandbox:latest -c '
    cp /source/skip_cases.py /source/capture.sv /source/Makefile /capture/ &&
    cd /capture &&
    test "$(cocotb-config --version)" = 2.1.0 &&
    COCOTB_RANDOM_SEED=1 make COCOTB_TEST_MODULES=skip_cases \
      > skip-unfiltered.log 2>&1;
    mv results.xml results-skip-unfiltered.xml &&
    COCOTB_RANDOM_SEED=1 make COCOTB_TEST_MODULES=skip_cases \
      "COCOTB_TEST_FILTER='\''^skip_cases\.(test_runtime_skip|test_runtime_skip_bare|test_runtime_skip_multiline)\$$'\''" \
      > skip-filtered.log 2>&1;
    mv results.xml results-skip-filtered.xml'
```

No substitutions were applied: the files are the emitted bytes. The suite
timestamp, the `/capture/...` file property, and the wall-clock ratio column
differ per capture, and no test reads them.
