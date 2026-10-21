---
title: Demos
document: Evaluation designs
---

<!-- Title block: same strip as the application note, labelled as an evaluation-kit document -->
<div class="strip"><span><b>EVAL</b>Evaluation designs</span><span>Booley product family &middot; booley-rtl</span></div>
<h1>Evaluation designs</h1>
<p class="lede">Open-source designs already set up for Booley. Clone one, open it in VS Code, and give the agent a task. They are examples, not the only designs Booley supports: you can set up any RTL project the same way.</p>

<section aria-labelledby="s1">
  <h2 class="sec" id="s1"><span class="n">1</span>Available designs</h2>
  <div class="tbl">
    <p class="tbl__cap">Table 1. Open-source designs already set up for Booley</p>
    <table>
      <thead><tr><th scope="col">Design</th><th scope="col">What it is</th><th scope="col">Notes</th><th scope="col">Booley project</th></tr></thead>
      <tbody>
        <tr id="picorv32">
          <td data-l="Design"><span><b>PicoRV32</b></span></td>
          <td data-l="What it is"><span>A small RISC-V CPU core (<a href="https://github.com/YosysHQ/picorv32">YosysHQ/picorv32</a>)</span></td>
          <td data-l="Notes"><span>Start here. Clone it, open it in VS Code, and type your first request to the agent.</span></td>
          <td data-l="Project" class="lnk"><span><a href="https://github.com/boldaxolotl/booley-prj-picorv32/blob/e53c1896ab02595aa4ebb3a56b5aa7bd109177f9/README.md">Open the PicoRV32 demo</a></span></td>
        </tr>
        <tr>
          <td data-l="Design"><span><b>Caliptra</b><sup class="fn"><a href="#fn1" aria-label="Note 1">1</a></sup></span></td>
          <td data-l="What it is"><span>The open-source hardware root of trust (<a href="https://github.com/chipsalliance/caliptra-rtl">chipsalliance/caliptra-rtl</a>): a full SoC with a RISC-V core and firmware</span></td>
          <td data-l="Notes"><span>Real large codebase. Caliptra&rsquo;s Verilator testbench, firmware tests, lint, and ASIC synthesis are wired into Booley Flows, so the agent can simulate the whole SoC and inspect its waveforms.</span></td>
          <td data-l="Project" class="lnk"><span><a href="https://github.com/boldaxolotl/booley-prj-caliptra#readme">Open the Caliptra project</a></span></td>
        </tr>
      </tbody>
    </table>
    <ol class="tbl__notes">
      <li id="fn1">1. Caliptra&rsquo;s Sandbox uses the RISC-V image and asks for 28 GB of memory.</li>
    </ol>
  </div>
  <p class="note"><b>Using your own design.</b> Booley is not limited to the designs in Table 1. Run <code>booley init</code> in your own RTL repository, then let the <code>booley-setup</code> skill plan and configure the project with you. Starting a new IP from scratch? <code>booley init --scaffold</code> writes a working starter project. See the <a href="https://github.com/boldaxolotl/Booley/blob/main/docs/user/SETUP.md">setup guide</a>.</p>
</section>

<section aria-labelledby="s2">
  <h2 class="sec" id="s2"><span class="n">2</span>Typical procedure</h2>
  <p class="proc__h" id="proc-h">Running your own change on a demo</p>
  <ol class="proc" aria-labelledby="proc-h">
    <li><span>Clone the configured demo project from Table 1.</span></li>
    <li><span>Open it in VS Code.</span></li>
    <li><span>Type a request to the agent, such as your own change to the design.</span></li>
  </ol>
  <p class="proc__f">Booley must be installed first: <code>pipx install booley-rtl</code>, then <code>booley bootstrap</code>. See the <a href="https://github.com/boldaxolotl/booley#installation">installation guide</a>.</p>
</section>

<section aria-labelledby="s3">
  <h2 class="sec" id="s3"><span class="n">3</span>See it run</h2>
  <p class="lede">Two <a href="../videos/">video walkthroughs</a> show an engineer driving Booley on a demo project, start to finish.</p>
</section>
