---
title: Demos
document: Evaluation designs
---

<!-- Title block: same strip as the application note, labelled as an evaluation-kit document -->
<div class="strip"><span><a class="strip__home" href="./"><b>EVAL</b>Evaluation designs</a></span><span>Booley product family &middot; booley-rtl</span></div>
<h1>Try Booley on a real chip design</h1>
<p class="lede">Open-source designs already set up for Booley. Open one in your browser, or clone it and work locally. They are examples: you can set up any RTL project the same way.</p>

<!-- Browser demo first: the one-click path needs no install, so it leads the page. -->
<div class="try">
  <div>
    <h2 class="try__h">Free in your browser, nothing to install</h2>
    <p>PicoRV32, a small RISC-V CPU, opens in browser VS Code with simulation, synthesis and waveform tools ready.</p>
    <p class="try__go">
      <a class="cta cta--inv" href="https://codespaces.new/boldaxolotl/booley-prj-picorv32?quickstart=1"><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>Open in GitHub Codespaces</a>
      <a href="../demo/">How the free demo works &rarr;</a>
    </p>
  </div>
  <dl class="try__facts">
    <div><dt>Cost</dt><dd>Free: fits GitHub&rsquo;s monthly Codespaces allowance</dd></div>
    <div><dt>Agent</dt><dd>GPT-6 Luna on us after a GitHub sign-in, $1 a month per account</dd></div>
    <div><dt>First start</dt><dd>3&ndash;4 minutes while the container image builds</dd></div>
    <div><dt>Upgrade</dt><dd>Connect your own Claude or ChatGPT account for a smarter model</dd></div>
  </dl>
</div>

<section aria-labelledby="s1">
  <h2 class="sec" id="s1"><span class="n">1</span>Available designs</h2>
  <div class="kits">
    <article class="kit kit--start" id="picorv32">
      <p class="kit__tags"><span class="tag tag--ok">Start here</span><span class="tag">Runs in browser</span></p>
      <h3 class="kit__h">PicoRV32</h3>
      <p>A small RISC-V CPU core (<a href="https://github.com/YosysHQ/picorv32">YosysHQ/picorv32</a>). Clone it, open it in VS Code, and type your first request to the agent.</p>
      <dl class="spec">
        <div><dt>Simulator</dt><dd>Verilator</dd></div>
        <div><dt>Flows</dt><dd>Simulation, lint, ASIC synthesis</dd></div>
        <div><dt>Sandbox</dt><dd>4 cores, 16 GB memory</dd></div>
      </dl>
      <p class="kit__go">
        <a class="cta cta--sm" href="https://codespaces.new/boldaxolotl/booley-prj-picorv32?quickstart=1"><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>Open in Codespaces</a>
        <a class="cta cta--sm cta--line" href="https://github.com/boldaxolotl/booley-prj-picorv32#readme">Clone from GitHub</a>
      </p>
    </article>
    <article class="kit">
      <p class="kit__tags"><span class="tag">Large SoC</span><span class="tag">Local only</span></p>
      <h3 class="kit__h">Caliptra</h3>
      <p>The open-source hardware root of trust (<a href="https://github.com/chipsalliance/caliptra-rtl">chipsalliance/caliptra-rtl</a>): a full SoC with a RISC-V core and firmware. The agent can simulate the whole SoC and inspect its waveforms.</p>
      <dl class="spec">
        <div><dt>Simulator</dt><dd>Verilator testbench and firmware tests</dd></div>
        <div><dt>Flows</dt><dd>Simulation, lint, ASIC synthesis</dd></div>
        <div><dt>Sandbox</dt><dd>RISC-V image, 28 GB memory</dd></div>
      </dl>
      <p class="kit__go">
        <a class="cta cta--sm cta--line" href="https://github.com/boldaxolotl/booley-prj-caliptra#readme">Clone from GitHub</a>
      </p>
    </article>
  </div>
  <p class="note"><b>Using your own design.</b> Booley is not limited to these designs. Run <code>booley init</code> in your own RTL repository, then let the <code>booley-setup</code> skill plan and configure the project with you. Starting a new IP from scratch? Run <code>booley init</code>, then <code>/booley-setup new</code>: it asks the design choices and writes a working starter project. See the <a href="https://github.com/boldaxolotl/Booley/blob/main/docs/user/SETUP.md">setup guide</a>.</p>
</section>

<section aria-labelledby="s2">
  <h2 class="sec" id="s2"><span class="n">2</span>See it run</h2>
  <p class="lede">Two <a href="../videos/">video walkthroughs</a> show an engineer driving Booley on a demo project, start to finish.</p>
</section>
