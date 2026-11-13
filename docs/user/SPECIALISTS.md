# Specialists

A **Specialist** is a helper (an LLM agent) that checks the Developer Agent's
work for one focused task. Booley runs a Specialist as a separate agent with
a fresh context. It didn't write the code and doesn't know the reasoning behind it, so
it judges the code as it is, not as it was meant to be. Use a Specialist when
you want a second opinion on RTL or tests. For a pass/fail verdict from an EDA
tool, use a [Flow](FLOW_REFERENCE.md) instead. Checking that your tests catch
planted bugs needs no LLM, so it is a Flow:
[mutation testing](FLOW-MUTATION.md).

Booley ships two:

- **[Reviewer](REVIEWER.md)** reviews RTL or testbench code for one concern at
  a time, such as bugs, protocol, or spec compliance.
- **Coverage Analyst** explains a coverage report and suggests what to test
  next. See [Analyzing a Campaign](COVERAGE.md#analyzing-a-campaign).

## Running a Specialist

Ask the agent, for example:

> Review the RTL for bugs.

The agent replies with the findings and the path to the report. In
[Goal Mode](GOAL-MODE.md), a Reviewer result can meet a review Goal; the
Coverage Analyst only advises.

To run one yourself, in a container terminal:

```bash
booley specialist                             # list the Specialists
booley specialist reviewer --help             # its options
```

Runs you start yourself never count as Goal evidence.

Specialists accept `-C/--project`, `--report-dir`, `--diagnostic`, and, where
it applies, `--target`. Two options exist only on the command line:

- `--max-turns N` caps the conversation length.
- `--timeout DURATION` limits each model call, not the whole run.

Reports go to `mcp-tool-reports/` in the Booley Directory, or under
`--report-dir`.

## Configuring Specialists

- Pin a Specialist to a model, and with it the Agent CLI it runs on, with
  [`[models]`](CONFIG.md#model-selection-models).
- Turn one off with
  [`enabled = false`](CONFIG.md#flow-and-specialist-availability-enabled).

[CLI-REFERENCE.md](CLI-REFERENCE.md#booley-flows--specialists) lists each
Specialist's arguments and the Goals it can satisfy.

If a Specialist misbehaves, report it as described in
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
