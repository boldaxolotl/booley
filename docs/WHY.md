# Why

[ARCHITECTURE.md](ARCHITECTURE.md) says *what* Booley is and *how* the pieces fit. This document says *why* the load-bearing pieces are the way they are: only the choices that would have produced a fundamentally different framework if made differently, with their costs stated honestly. These don't get revisited without rebuilding.

This doc assumes Booley's vocabulary — Target, Booley Flow, Specialist, EDA Provisioning, Ticket Mode — rather than redefining it. [CONTEXT.md](CONTEXT.md) is the glossary if a term is unfamiliar; you don't need to read ARCHITECTURE.md first.

## Why Docker

Booley runs LLM agents that execute real EDA flows against your RTL, exactly what you don't want loose on your machine. Docker provides the Session Runtime boundary: the agent has no direct host command channel and no ambient network. A misbehaving or hijacked agent can still corrupt the working copy it was handed, so review remains essential. A built-in host-provisioning policy can expose an approved installation read-only and, when required, narrowly scoped license connectivity without changing where EDA processes execute; see [ARCHITECTURE.md](ARCHITECTURE.md).

The container earns its keep a second time as a unified development environment: the entire open-source stack (Verilator, Icarus, Yosys, OpenROAD, sv2v, `bwave`, Python) ships pre-installed in the image. You provision nothing on your machine, and everyone runs the same pinned stack, so "works on my machine" stops being a category of bug.

The expected performance cost did not materialize in measurement. On a 16-core, 24-thread Intel Core i7-14650HX workstation with 32.8 GB of RAM, a controlled Ibex campaign found no measurable Docker overhead. It covered all four supported Ibex configurations with clean, warm, direct-runtime, and traced Verilator simulation, plus the three configurations that completed the Yosys/OpenROAD synthesis flow. Each workload used one excluded warmup and five alternating, CPU-pinned container/host pairs: 190 measured legs and 228 successful legs including warmups. Pooled median paired differences ranged from -0.13% to +0.20%, and every per-configuration median stayed between -0.45% and +0.98%—within ordinary run-to-run variability. On this system, the container provided isolation and reproducibility without a demonstrated EDA runtime penalty.

## Why one container

Docker settled *where* agent work runs; it didn't settle *how many* containers. The road most traveled, a fresh ephemeral sandbox per task, as cloud coding agents (Codex cloud, Devin) and research harnesses (SWE-agent, OpenHands) do, was rejected twice, for two different reasons.

Interactive Mode settled it first, on implementation cost. The Dev Containers extension's natural unit is one container per opened VS Code window; anything finer (a container per chat tab) means session-to-container plumbing the extension doesn't have. One container per opened folder was the version that was easy to build, so that's the version that exists.

Ticket Mode then chose to run inside that *same* container for two reasons. Similarity: the closer its environment is to Interactive Mode's, the more code the two share (one MCP server, one EDA stack, one config path), and shared code gets debugged once; a separate per-ticket container would have doubled the environments to keep working. And the single window: `booley run` is just another terminal tab next to your interactive session, inside the one VS Code window you already have open; outside the container, driving it would mean a separate terminal outside the IDE.

The cost is real: a shared container makes admission control mandatory — a slot budget that decides how many tickets plus the interactive session may run at once, because they all draw on one memory budget — and every worktree in it sits in one trust domain. Both consequences are handled, not free; see [ARCHITECTURE.md](ARCHITECTURE.md#the-sandbox) for the machinery.

## Why VS Code

VS Code is the interactive front end because it collapses three needs into one program:

- One editor for both agents. VS Code has mature extensions for both Claude Code and Codex, the two agent backends Booley supports: a single application to install and drive.
- Sandbox sessions for free. The Dev Containers extension makes "open my repo *inside the Booley container*" a trivial, first-class operation. Interactive Mode's whole premise (chat with an agent that lives in the same sandbox the Booley Flows run in) falls out of an extension that already exists, rather than something we had to build.
- It's a genuinely good editor. Hardware design is not pure delegation: you will read and edit RTL yourself between agent turns, and the agent and the human share one workspace instead of context-switching between EDA tools.

Together these make VS Code the shell of an *agentic RTL IDE*: editor, sandboxed toolchain, and agent in one window. RTL work has never had an IDE. An ASIC engineer today lives in a code editor with a stack of terminals firing heavy EDA tools by hand, and this is where Booley builds one.

## Why two modes

Two ways to drive one sandbox complicates the UI, and that is a real cost. We pay it because each mode covers a job the other structurally cannot.

Interactive Mode is not optional; it's inevitable. Hardware work is full of tasks where step B depends on what you learned in step A: explore a design, measure something, look at a waveform, decide the next move from the result. That can't be packaged into a ticket: the plan is discovered as you go. Any honest hardware-design workflow *requires* a hands-on, tight-loop mode. This is the floor.

Ticket Mode is aspirational, but useful today: whenever you have a **well-scoped task that runs long**, you write a self-contained ticket, `booley run` drives it to completion unattended, and you review the result later. This is how we *want* to work with agents: hand off a bounded job, come back to reviewable output. The machinery that makes that trustworthy, **machine-checkable acceptance criteria** and **explicit scope**, exists precisely to keep a long-running agent from drifting off its goal.

Ticket Mode also seeds a longer-term ambition: **design-IP-from-spec.** Once tickets are the trustworthy unit of unattended work, a manager agent can *author* tickets for worker agents to execute, the ticket becoming the shared language between them. Ticket Mode is the down payment on that future.

## Why FuseSoC

Booley owns the build system, the layer that turns a design into the exact command that invokes each EDA tool, and that ownership is what makes criteria and the MCP server possible. Machine-checkable acceptance criteria are structured readings of a Booley Flow's result, and you can only read a result you produced from an invocation you controlled; the single-call MCP surface (below) is a typed wrapper over that same invocation. Both are downstream of one decision: Booley, not the user's prompt, decides how Booley Flows run.

We don't hand-roll that build system. FuseSoC is the most mature open-source EDA build system out there, sitting directly on Edalize (which generates the per-EDA-tool command): one stack, one set of maintainers. The undifferentiated plumbing every EDA build needs (file-list ordering, typed parameters, per-flow targets, the long tail of EDA tool integrations) is code Booley never writes and FuseSoC maintains better.

The cost is real: every project needs a FuseSoC `.core` target before Booley can build it: one-time setup friction in exchange for zero per-call drift. We pay it deliberately rather than letting the agent pass ad-hoc files and defines per invocation, because that escape hatch would resurrect exactly the source-discovery code the FuseSoC adoption deleted. In practice the tax is small: the `.core` setup is quick and mechanical even for a codebase that doesn't use FuseSoC today, and the setup skills walk you through it.

FuseSoC is mandatory, and deliberately so: eight ports showed a `.core` is a mechanical restatement of the filelists a project already has, and the one thing it can't express — a per-test non-RTL build step — is `[flows.sim].pre_run_commands`, not a parallel build path. One builder means the criteria system never has to trust anyone else's verdict.

## Why an MCP server

The agent reaches every Booley Flow through a single MCP (Model Context Protocol) call, not by composing shell commands in a prompt. MCP is the standardized MCP tool-calling channel that Claude Code and Codex both speak natively, so a capability offered as an MCP tool is one the model was trained to invoke — a typed function call, not prose the agent has to parse into a shell command. That native channel buys five things CLI-in-prompt can't match. See [`server.py`](../src/booley/mcp/server.py) for the mechanics:

- **It's the interface the model was trained on.** The same capability lands far more reliably as a typed MCP tool definition than as prose: the model is fine-tuned to call native MCP tools ([Anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works) and [OpenAI](https://openai.com/index/function-calling-and-other-api-updates/) both say so outright), whereas a capability described in the prompt competes for attention with everything else in context. (The gap is measurable. [MCPVerse](https://arxiv.org/abs/2508.16260) moved identical MCP tool definitions out of the native MCP-capability block into the prompt and watched Claude 4 Sonnet drop from 62% to 15-36% task accuracy, fabricating MCP tool responses over 70% of the time; the [Berkeley Function Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html) scores every model both ways, frontier models consistently ahead in native mode. A meticulously documented CLI can close the success-rate gap — [one 120-run Claude Code benchmark](https://mariozechner.at/posts/2025-08-15-mcp-vs-cli/) showed exactly that — but at ~40× the tokens in shell round-trips, and "meticulously documented" never survives contact with maintenance.)
- **Schema fidelity, zero drift.** Each MCP tool's schema is auto-extracted from its own argparse parser (`extract_schema(instance._parser)`), so the agent gets exact, typed parameters with no `--help` round-trips and no prompt docs to keep in sync. CLI-in-prompt forces a choice between hand-maintained docs (which drift) and burning a Bash call per MCP tool just to read `--help`.
- **Output curation.** An MCP result is the curated triple: exit code + tail-truncated stdout (capped at 12 KB) + the parsed fields from `report.json` merged in (`_format_tool_result`). Through raw Bash the agent gets full unstructured output and has to *remember* to go read `report.json` itself: more steps, more context burned, more run-to-run variance.
- **Visibility gating that actually hides.** `BOOLEY_MCP_TOOLS`, the nested-agent allowlists, and the interactive-mode exclusions (`_mcp_tool_visible`) genuinely remove MCP tools from what the agent can see and call. The CLI equivalent is prompt discipline: a request, not an enforcement.
- **Native MCP-tool-call ergonomics.** Models hallucinate flags far less on structured MCP tool calls than on hand-composed shell strings with their quoting and escaping. The exception proves the rule: `bwave` needs a hand-authored schema *precisely because* its argparse `REMAINDER` doesn't map cleanly to structured params. The one MCP tool that fights the abstraction is the one that has to be special-cased.

## Why plain files

Every piece of Booley state is a file: the ticket board is directories with atomic moves, run state is persisted JSON, the job slot store lives on disk, and a run's durable artifact is a git branch. No database, no state daemon. The reason is simplicity, literally: nothing to install, nothing to keep running, nothing that can be down, and every piece of state can be inspected with `ls` and `cat` and recovered with git. The cost is that filesystem semantics (atomic renames, lock files) become our concurrency primitives, which takes care to get right: a trade that works at one-human-per-project scale, which is the scale Booley targets.

## Why VCD in, FST out

Every traced run takes the same path: the simulator dumps VCD, and `bwave build` converts it to [FST](https://github.com/gtkwave/gtkwave). There is no second path. That is two separate decisions.

**VCD is the input because the supported simulators speak it.** Verilator and Icarus share almost nothing, but both dump VCD. Some can write FST natively (Icarus `-fst`, Verilator `--trace-fst`), but "some" is the problem. Taking VCD as the universal input buys one trace path for the supported image-provisioned simulators. The alternative is a per-simulator matrix of trace formats, each with its own quirks, plus a VCD fallback anyway for the ones that don't cooperate.

**FST is the store because VCD is unusable as one.** A VCD is a text log with no index: answering "what was this signal at time T" means parsing from the top, and at 10 GB that is a batch job, not a query. FST is transition-based, indexed, and typically 10-50x smaller, which is what makes `bwave`'s whole query surface (time-range reads, trigger sampling, data tracing) fast enough for an agent to iterate against. It is also open, documented, and already read by GTKWave, Surfer, and VaporView, so interop costs nothing and `bwave gui` needs no export step. 

The cost is real: **an encode and a decode on every traced run, even when the simulator could have written FST itself.** The FIFO trick takes the sting out: the simulator writes VCD into a named pipe, `bwave` converts in parallel, and the multi-GB intermediate never touches disk, so what's left is CPU that overlaps with the run. But it isn't zero, and on Verilator it's a conversion we could in principle skip.
