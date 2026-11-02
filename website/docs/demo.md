---
title: Free demo
document: Free browser demo
---

<!-- booley.dev/demo is the link the demo gateway prints in its notice and its
     "allowance used up" errors; keep the #bring-your-own-key anchor. -->
<div class="strip"><span><a class="strip__home" href="./"><b>DEMO</b>Free browser demo</a></span><span>Booley product family &middot; booley-rtl</span></div>
<h1>Try Booley free in your browser</h1>
<p class="lede">One click opens Booley with the PicoRV32 RISC-V CPU in browser VS Code, with simulation, synthesis and waveform tools already installed. Nothing to install, and nothing to pay.</p>

<p><a href="https://codespaces.new/boldaxolotl/booley-prj-picorv32?quickstart=1"><img src="https://github.com/codespaces/badge.svg" alt="Open in GitHub Codespaces"></a></p>

<section aria-labelledby="s1">
  <h2 class="sec" id="s1"><span class="n">1</span>How the free demo works</h2>
  <ul>
    <li>The Codespace runs on your GitHub account's free monthly Codespaces allowance. The first launch takes 3-4 minutes while the container image builds.</li>
    <li>The agent runs OpenAI's GPT-6 Luna through the Booley demo gateway, on the Booley maintainer's account. The first terminal shows a short notice; you agree and sign in with GitHub to get a demo token. The gateway logs usage numbers, never your prompts or code.</li>
    <li>Each token has a monthly allowance: plenty to run a simulation, read a waveform and synthesize the core. The Zbb Goal Mode walkthrough is a bigger job and may use it up.</li>
    <li><a href="https://github.com/boldaxolotl/booley-prj-picorv32/blob/main/CODESPACES.md">CODESPACES.md</a> opens in the Codespace and walks you through the rest.</li>
  </ul>
</section>

<section aria-labelledby="bring-your-own-key">
  <h2 class="sec" id="bring-your-own-key"><span class="n">2</span>Bring your own key</h2>
  <p>Your own Claude or ChatGPT account gives you a stronger model and no demo allowance. In the Codespace, open a terminal and sign in with one of:</p>
  <pre><code>BROWSER=none claude auth login
codex login --device-auth</code></pre>
  <p>Then switch Booley to your account (with both signed in, it picks Claude Code):</p>
  <pre><code>python -m booley.harness.incontainer_register</code></pre>
  <p>CODESPACES.md has the <a href="https://github.com/boldaxolotl/booley-prj-picorv32/blob/main/CODESPACES.md#or-use-your-own-claude-or-chatgpt-account">step-by-step sign-in</a>. To run Booley on your own machine instead, see the <a href="docs/install/">Install guide</a>.</p>
</section>
