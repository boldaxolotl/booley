# Cocotb timeout progress sample

`2.1.0/mixed.log` and `2.1.0/parametrized.log` are the complete console output
of two producer batches killed while a test was still running. They feed the
console-based timeout recovery tests, which previously used authored progress
lines only. Ordinary pytest runs read the logs and require neither Cocotb nor a
simulator.

Cocotb 2.x logs each test as `<module>.<test>`, while `results.xml` and
selected tests use the bare name. A generated parametrized name carries dots of
its own (`test_gain/gain=0.5`), so the module path cannot be removed by
splitting at the last dot.

## Provenance

Captured on 13 OCT 2026 from the included `capture.sv`, `mixed_cases.py`,
`parametrized_cases.py`, and `Makefile`, through Cocotb's normal Icarus
regression:

- Cocotb **2.1.0**, verified with `cocotb-config --version` and the runtime log.
- Icarus Verilog **13.0 (stable)**, version output `dfeee90-dirty`.
- Embedded Python **3.14.0**, as reported by the simulator log.
- Local Sandbox Image `booley-sandbox:latest`, image ID
  `sha256:8c32490574407ceef7bf79163e5cd74002c4ef3c4b1ac8b7883b0a4c95495f57`.
- Seed `1` was fixed at generation time.

From the repository root, with that image available:

```sh
capture_dir=$(mktemp -d)
docker run --rm --network none --hostname cocotb-timeout-progress \
  --user "$(id -u):$(id -g)" --entrypoint sh \
  --mount type=bind,src="$PWD/tests/fixtures/cocotb_timeout_progress",dst=/source,readonly \
  --mount type=bind,src="$capture_dir",dst=/capture \
  booley-sandbox:latest -c '
    cp /source/mixed_cases.py /source/parametrized_cases.py \
      /source/capture.sv /source/Makefile /capture/ &&
    cd /capture &&
    test "$(cocotb-config --version)" = 2.1.0 &&
    for scenario in mixed parametrized; do
      rm -f results.xml
      PYTHONUNBUFFERED=1 COCOTB_RANDOM_SEED=1 \
        COCOTB_TEST_MODULES=${scenario}_cases \
        timeout -s KILL 20 make > ${scenario}.log 2>&1
    done'
```

Each `timeout` exits **137** and leaves no `results.xml`. The batch must be
killed with `KILL`: on `TERM` the simulator may shut down in order, and Cocotb
then reports the hanging and the never-started tests as failed and writes a
complete result document, which is a different scenario.

- `mixed`: `test_done` passes, `test_bad` fails an intentional assertion,
  `test_hang` never completes, and `test_later` never starts.
- `parametrized`: `test_gain/gain=0.5` passes and `test_gain/gain=1.5` never
  completes.

## Sanitization

None. The logs are byte-exact; they carry no timestamp, hostname, or host path.
`/capture` and `/usr/local/...` are paths inside the capture container.
