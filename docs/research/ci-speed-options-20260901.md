# CI speed options — 2026-09-01

This report combines first-party GitHub run data, the checked-in workflows, and
official runner-provider documentation. Numbers described as **measured** come
from Booley's jobs and APIs; **verified** facts come from official documentation;
provider speedups remain **vendor claims** until Booley benchmarks them.

## Decision

Make the repository-side changes first. They attack the measured critical paths
without charging for compute:

1. Benchmark pytest-xdist `--dist=load` and `--dist=worksteal` on Windows. The
   present `loadscope` scheduler serializes nearly the entire slowest module on
   one worker.
2. Prototype remote OCI manifest/config validation so `bwave-smoke` does not
   pull the multi-gigabyte stable image only to inspect its digest and label.
3. Stop writing the large BuildKit cache independently from every PR ref; make
   `main` the cache writer and benchmark PRs as readers. Increase the repository
   cache limit from 10 GB to 50 GB with a hard budget while measuring the new
   policy.
4. Move the sidecar control-image evidence and the non-required PicoRV32 demo
   off the all-change PR critical path, subject to the intended assurance policy.

The immediate server-side purchase is **GitHub Pro for the repository owner,
$4/month**, if bursts like the measured week are normal. It raises the standard
hosted-runner account cap from 20 to 40 concurrent jobs, directly addressing the
observed queueing. It does not make an individual job faster. Standard Ubuntu
and Windows jobs in this public repository already cost $0, so buying compute
for every job is unlikely to be good value
([GitHub plan FAQ](https://docs.github.com/en/get-started/learning-about-github/faq-about-changes-to-githubs-plans),
[Actions limits](https://docs.github.com/en/actions/reference/limits)).

GitHub larger runners and the two preferred managed-runner alternatives are not
available to the repository in its current ownership form. The GitHub repository
API identifies `boldaxolotl/booley` as owned by a **User**, while GitHub larger
runners require a Team/Enterprise **organization**, and both Blacksmith and
Depot explicitly reject personal-account repositories
([repository API](https://api.github.com/repos/boldaxolotl/booley),
[GitHub larger-runner eligibility](https://docs.github.com/en/actions/reference/runners/larger-runners),
[Blacksmith quickstart](https://docs.blacksmith.sh/introduction/quickstart),
[Depot overview](https://depot.dev/docs/github-actions/overview)). Transfer to a
GitHub organization is therefore a prerequisite, not a workflow-label change.

If that governance change is acceptable, **Blacksmith is the best first pilot**,
limited initially to `bwave-smoke`. Its local persistent BuildKit cache is a
particularly close match for Booley's six-gigabyte image build-and-load job.
Depot is the best second bid. Do not migrate the full matrix until an A/B trial
shows a material wall-time improvement and acceptable public-PR cache isolation.

## What is slow now

### End-to-end and account saturation

Seventeen recent successful `Tests` runs that did not wait for an account slot
had a **10m04s median wall time**, ranging from 8m34s to 12m30s. The latest
representative run, [33494169982](https://github.com/boldaxolotl/booley/actions/runs/33494169982),
took 9m19s. A representative PR consumed 41m22s of runner time inside `Tests`;
a full `main` run consumed 45m50s. Thus the roughly ten-minute response time is
already the result of broad fan-out, not a ten-minute sequential workload.

A full `Tests` run can expose about 12 simultaneous jobs (nine Ubuntu and three
Windows); PicoRV32 and confidential-content add two more. At 10:01:23 UTC in the
sampled period, the account had exactly 20 jobs running and newly eligible jobs
waited one to two minutes. Two saturation-affected workflow runs consequently
took 20m51s and 27m04s. This is the effective **CI jobs cap**: GitHub documents
20 concurrent standard hosted jobs on Free and 40 on Pro
([Actions limits](https://docs.github.com/en/actions/reference/limits)).

The week beginning 2026-08-25 was unusually active: the Actions API returned
312 `Tests` runs (165 successful, 64 cancelled, 79 failed), 318 PicoRV32 runs,
and 339 confidential-content runs. Same-ref cancellation is already configured,
but distinct branches and workflows still compete for the account-wide cap.
The representative run's billing-timing API reported `total_ms: 0` for both
Ubuntu and Windows, directly confirming that the present hosted compute was not
billed
([timing API](https://api.github.com/repos/boldaxolotl/booley/actions/runs/33494169982/timing)).

### Windows test imbalance

The representative run's pytest steps were:

| Matrix leg | Test-step wall time |
| --- | ---: |
| Windows Python 3.11 | 431s |
| Windows Python 3.13 | 367s |
| Windows Python 3.14 | 287s |
| Linux Python 3.11 | 144s |
| Linux Python 3.13 with coverage | 214s |
| Linux Python 3.14 | 156s |

The run's JUnit reports contain 8,894 cases. On the three Windows legs,
`tests.ticket_board.test_completion` accounts for 390s, 337s, and 261s of suite
wall time respectively. The workflow invokes `pytest -n 4 --dist=loadscope` in
[`test.yml`](../../.github/workflows/test.yml), and the module contains repeated
isolated Git repositories and a large parametrized crash-boundary matrix.

This is an expected scheduler effect, not evidence that Windows needs a more
expensive machine. pytest-xdist documents that `loadscope` groups all functions
in a module (or methods in a class) onto one worker, while `load` feeds tests to
available workers and `worksteal` redistributes tests from workers with long
queues to idle workers
([xdist distribution modes](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)).
Run the same Windows commit repeatedly with `loadscope`, `load`, and `worksteal`
before changing fixture structure. If broad scheduling is unsafe, mark only the
tests that truly share state or split the completion module into smaller stable
groups.

### Docker smoke critical path and cache pressure

`bwave-smoke` took **7m56s** in run 33494169982:

| Measured segment | Wall time |
| --- | ---: |
| Pull stable base for digest/contract resolution | 92s |
| Build, load, and cache candidate with BuildKit | 184s |
| No-cache sidecar control/candidate builds | 18s |
| Sidecar behavior proofs | 70s |
| Eight-way final image validation | 79s |

Another run measured 107s, 189s, 23s, 69s, and 75s for the same segments. A
full `main` run that rebuilt the stable base spent 303s on it and 78s on the
candidate, reaching 9m59s for the job.

`resolve_image` in
[`docker_base_contract.py`](../../src/booley/harness/docker_base_contract.py)
currently executes `docker pull` to verify the remote RepoDigest and image
config label. Buildx subsequently downloads/loads through its own content store.
Docker's official `buildx imagetools inspect` interface can inspect a remote
manifest digest and format the remote image configuration without pulling all
layers into the local daemon
([Docker reference](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/)).
That makes a remote-metadata verifier a credible way to remove the measured
92–107 second duplicate transfer. It remains a prototype until it proves the
same immutable digest and contract-label guarantees, including multi-platform
selection and registry error behavior.

At 10:21 UTC on 2026-09-01, the GitHub cache API reported
**10,488,758,535 bytes (9.77 GiB) in 121 active entries**, close to the default
10 GB cap
([usage API](https://api.github.com/repos/boldaxolotl/booley/actions/cache/usage)).
The largest-entry listing showed the same 1,864,214,976-byte BuildKit object
under `main` and multiple PR refs, plus several 700 MB-class BuildKit objects and
448–646 MB pip caches
([cache API](https://api.github.com/repos/boldaxolotl/booley/actions/caches?per_page=100&sort=size_in_bytes&direction=desc)).
GitHub explicitly warns that a repository at its cache limit can thrash by
creating and immediately evicting caches. Paid users can opt into a repository
limit as high as 10 TB; usage over 10 GB costs $0.07/GB-month
([cache limits](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
[Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)).

Therefore add a payment method, set a 50 GB repository cache ceiling, and set a
separate low Actions Cache Storage budget. Holding 50 GB for a full month would
cost at most about **$2.80/month above the free 10 GB**. More capacity alone can
hide waste, so combine it with the `main`-writer/PR-reader experiment and inspect
cache hit, restore, export, and eviction timings after a week.

### Work that need not occupy every PR slot

The sidecar migration control matrix still builds all control and candidate
images without cache on every `bwave-smoke` run. Move the whole sidecar
validation into a separate path-gated or parallel job: preserve candidate
behavior checks whenever sidecar code or contracts change, while moving the
historical control-image comparison to a scheduled/release evidence lane unless
every source change must re-prove it. Separating the two measured sidecar steps
removes roughly 88 seconds from the `bwave-smoke` critical path even when the
sidecar job remains required for a relevant change.

[`picorv32-demo.yml`](../../.github/workflows/picorv32-demo.yml) runs on every PR
and push plus a schedule, takes about 2.4–3.6 minutes, and consumes one Ubuntu
slot. The active repository ruleset requires `ci-required` and
`confidential-content`, not PicoRV32. Either promote Pico to an intentional
required contract, or path-gate it and retain `main`/schedule coverage. Running
a non-required demonstration on every source change increases queue pressure
without shortening merge feedback.

The current design already has good controls worth preserving: a path
classifier, pip caching, four xdist workers, coverage folded into Linux 3.13,
same-ref cancellation, and a single stable aggregate required check.

## GitHub-hosted options

The public repository's standard Ubuntu and Windows runners are already four
CPU/16 GB virtual machines and are free and unlimited by minutes. The smaller
`ubuntu-slim` label is one CPU, unprivileged, subject to a 15-minute timeout,
and cannot perform Docker-in-Docker; it shares the same plan concurrency model.
It can reduce private-repository cost, but is not a speed lever here
([hosted-runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)).

| Option | Concurrency / hardware | Current cost and eligibility | Booley assessment |
| --- | --- | --- | --- |
| Personal GitHub Free | 20 standard jobs; current public 4-CPU Ubuntu/Windows | $0 | Queueing is measured at this cap. |
| Personal GitHub Pro | 40 standard jobs, same hardware | $4/month | **Buy first if burst traffic is normal.** No organization transfer; fixes queueing, not job duration. |
| Organization GitHub Team | 60 standard jobs; unlocks larger runners | $4/user/month | Requires repository transfer. Useful only if organization governance or larger runners are desired. |
| GitHub larger runners | 8 CPU/32 GB/300 GB is the first clear upgrade over today's public runner | Team/Enterprise organizations only; Linux 8 CPU $0.022/min, Windows 8 CPU $0.042/min; always charged for public repos | Cleanest high-trust speed trial after transfer; benchmark Windows tests and `bwave-smoke`, do not assume linear scaling. |

GitHub's official limits are 20/40/60/500 standard concurrent jobs for
Free/Pro/Team/Enterprise, and up to 1,000 for larger runners; Support can accept
requests to increase job-concurrency limits
([Actions limits](https://docs.github.com/en/actions/reference/limits)). GitHub
lists Team at $4/user/month and Enterprise starting at $21/user/month
([GitHub pricing](https://github.com/pricing)). Larger runners are billed by
whole minutes, cannot use included minutes, and are not free for public
repositories; current x64 rates include Linux 4/8/16 CPU at
$0.012/$0.022/$0.042 per minute and Windows at
$0.022/$0.042/$0.082
([runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)).

For this workload, an 8-CPU larger runner is more defensible than a 4-CPU one:
the existing public runners already expose four CPUs. The 8-CPU Windows test
trial should raise xdist workers and verify that the completion suite actually
fans out; otherwise more cores will idle. The Linux Docker trial may benefit
from faster CPU and much larger local SSD, but still starts cold and still needs
an external BuildKit cache. Custom runner images are unlikely to matter yet
because ordinary setup is only tens of seconds and the large application image
is produced and executed inside the job.

## Managed runner shortlist

All prices and features below were checked on 2026-09-01. Performance figures
from providers are explicitly vendor claims, not expected Booley results.

### 1. Blacksmith — recommended post-transfer pilot

Blacksmith offers Ubuntu x64/ARM, Windows x64 (currently **public beta**), and
macOS. It lists 2-CPU Ubuntu x64 at $0.004/min and Windows at $0.008/min, with
larger sizes charged in proportional 2-CPU-minute units, 3,000 free x64 2-CPU
minutes per organization each month, and no provider concurrency cap
([pricing](https://www.blacksmith.sh/pricing),
[instance types](https://docs.blacksmith.sh/blacksmith-runners/overview)). Its
claims of 2x runtime speed, sub-three-second provisioning, 4x cache downloads,
and 2–40x Docker improvements are vendor measurements and customer reports,
not guarantees for Booley.

Migration of an ordinary job is one `runs-on` label change. The Docker advantage
requires replacing the Docker setup/build actions with Blacksmith's actions and
removing the current `cache-from`/`cache-to`. Its optional repository-shared
persistent BuildKit cache uses a local builder and sticky storage, commits at
successful job end, follows last-write-wins for concurrent writers, can be size
capped, and costs $0.50/GB-month
([Docker cache documentation](https://docs.blacksmith.sh/blacksmith-caching/docker-builds)).
This avoids exporting/importing the multi-gigabyte cache on every run and keeps
the candidate local for Booley's subsequent image tests, hence the fit for
`bwave-smoke`.

Open issues for the pilot:

- The Windows service is beta and omits some components from GitHub's image.
  Linux Docker containers are not supported on its Windows runner; Booley's
  current Windows legs are Python-only, so that limitation is acceptable but
  must stay true.
- The Docker cache is shared by all runners in a repository. The documentation
  describes repository isolation and ephemeral cache tokens, but not branch or
  fork write isolation. Confirm with Blacksmith how public fork PRs are prevented
  from poisoning a cache later consumed by trusted jobs, or use a trusted-branch
  writer/read-only-PR design.
- The listed OSS program asks for an actively maintained public repository,
  permissive license, and clear community use. Booley's Apache-2.0 license makes
  an application reasonable, but acceptance and benefits are discretionary and
  not published as an entitlement.

### 2. Depot — strongest fallback

Depot requires organization ownership and changes only the runner label for
ordinary jobs. It supplies Linux, Windows, and macOS, has no provider concurrency
cap, and says every job gets a new, never-reused single-tenant EC2 instance. The
Developer plan is $20/month with 2,000 base minutes and 25 GB cache; Linux
2 CPU/8 GB costs $0.004/min, Linux 4 CPU/16 GB $0.008/min, and equivalent Windows
costs $0.008/$0.016
([overview](https://depot.dev/docs/github-actions/overview),
[runner types](https://depot.dev/docs/github-actions/runner-types)). Its “up to
3x runner” and “10x cache” numbers are vendor claims.

Depot transparently redirects GitHub cache clients to a repository-scoped cache,
but does **not** isolate by branch; cache keys must provide that trust boundary
([cache integration](https://depot.dev/docs/cache/integrations/github-actions)).
Depot's remote container builders provide persistent BuildKit cache and can be
co-located with its runner, but Booley loads and exercises the resulting image
locally, so result-transfer time may offset remote-build gains. Trial it only if
Blacksmith's beta Windows support, cache model, or measured result is unsuitable.

### 3. Namespace — capable, but too large a first step

Namespace offers ephemeral managed runners, Linux/Windows, remote Docker builders
with persistent caching, and a 30-day Developer trial. Developer is pay-as-you-go
with 32 Linux-vCPU concurrency, while Windows is generally available only on the
$100/month Team plan; Team includes 100,000 unit-minutes and 64 Linux-vCPU
concurrency. Current overage rates include Linux 4 CPU/8 GB at $0.006/min and
Windows at $0.012/min
([pricing](https://namespace.so/pricing)). Its Docker integration requires
removing the existing Buildx setup so Docker commands use a remote builder; its
documentation recommends considering local caching for images that need to be
loaded or are very large
([Docker optimization](https://namespace.so/docs/solutions/github-actions/docker-builds)).
The $100 floor for retained Windows support and greater migration surface make
it a third choice at Booley's present scale.

## Security and trust implications

Do not put a long-lived self-hosted machine behind this public repository.
GitHub says self-hosted runners should “almost never” be used for public repos
because a fork PR can execute untrusted code and persistently compromise the
environment; clean ephemeral isolation is the relevant requirement
([GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use)).
The announced $0.002/min self-hosted Actions platform charge was postponed, so
current official documentation still describes self-hosted Actions usage as
free; compute and operations remain the owner's cost
([GitHub pricing update](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/),
[current billing](https://docs.github.com/en/actions/concepts/billing-and-usage)).

Managed providers reduce persistence risk, but they add a vendor data plane,
GitHub App, cache, logs/analytics, subprocessors, and contractual availability
to the trust boundary. Blacksmith says Linux and Windows jobs run in ephemeral
Firecracker microVMs and its GitHub App cannot directly read secrets, but the App
requests read/write access to Actions, code, pull requests, and workflows plus
organization runner management
([Blacksmith security](https://www.blacksmith.sh/security)). Depot says each job
runs on a new EC2 instance that is destroyed afterward and caches are encrypted,
while also warning that anyone allowed to build a project can read or modify its
cache
([Depot security](https://depot.dev/docs/security)). These are first-party
architecture statements, not an independent audit.

For any pilot:

- Install the provider App only on Booley and review every requested permission.
- Keep `confidential-content`, which runs in a privileged event context, on
  GitHub-hosted runners until its token and data flow receive a separate threat
  review.
- Preserve read-only tokens and no secrets for untrusted fork PRs, first-time
  contributor approval, immutable action pins, budgets, and cancellation.
- Require an explicit answer about fork-to-trusted cache visibility. Prefer a
  cache written only by `main`, or keys that prevent an untrusted write from
  becoming a trusted restore.
- Review log/cache retention, deletion, regions, incident response, SOC reports,
  subprocessors, and export/exit behavior before broad installation.

## Implementation and pilot order

1. **Repository A/B tests:** run the three xdist schedulers repeatedly on the
   same Windows commit; prototype remote OCI metadata inspection; measure a
   `main`-writer/PR-reader BuildKit cache; move control-sidecar and Pico work
   according to the chosen assurance policy.
2. **Capacity and cache:** if 20-job saturation remains common, buy personal
   GitHub Pro. Independently enable a 50 GB cache cap with a low hard budget.
   Compare at least 20 eligible runs before and after using queue time, workflow
   median/p95, cache hit/export time, and failure/cancellation counts.
3. **Governance decision:** transfer to a GitHub organization only if the team
   wants organization ownership independently of CI, or measured remaining delay
   justifies larger/third-party runners. GitHub Team is then the least novel
   8-CPU trial; Blacksmith is the best cache-focused trial.
4. **Blacksmith smoke pilot:** route only `bwave-smoke` to 4-CPU and 8-CPU Linux
   runners in separate comparable trials. Measure queue, checkout/setup, stable
   resolution, BuildKit build/load, image validation, total wall time, billed
   2-CPU-minute units, and sticky-cache GB. Test cold and warm caches.
5. **Expansion gate:** expand only if the pilot reduces median `bwave-smoke` by
   at least 25%, improves end-to-end p95 during burst traffic, stays within a
   declared monthly budget, and introduces no new flaky, image-contract, or
   public-cache trust failures. Trial Windows separately because it is beta and
   because scheduling—not yet runner speed—is the measured bottleneck.

This sequencing preserves the current $0 public compute benefit, purchases the
specific capacity and cache limits Booley has actually hit, and turns provider
claims into Booley-specific evidence before accepting organization-transfer,
security, and recurring-cost commitments.
