# Sandbox isolation (red-team) tests

Manual red-team checks that the `booley-sandbox` Docker image cannot be escaped,
used to exfiltrate data, or DoS the host. These require a **built image** and a
working Docker daemon, so they are not part of the `pytest` unit suite and are not
collected automatically.

Build the image first:

```bash
src/booley/data/docker/build.sh
```

Then run either entry point:

```bash
# Shell version
tests/docker/isolation/test_isolation.sh [--verbose]

# Python version (identical checks; avoids the agent bash-hook pattern matching)
python tests/docker/isolation/run_isolation_tests.py [--verbose]
```

Exit 0 = all isolation checks pass. Non-zero = the sandbox is broken.

> Previously lived under `src/booley/data/docker/`, where they were wrongly shipped
> as packaged wheel data. They are dev/CI artifacts, not runtime payload.
