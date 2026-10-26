---
title: Documentation
document: User guide
description: The Booley user guide - setup, day-to-day use, Goal Mode, Flows, configuration, the Sandbox, supported EDA tools, Doctor, and troubleshooting.
---

# Documentation

The Booley user guide. The first time, read **Get started** in order.

<!-- Section cells span their rows, which Markdown tables cannot do. -->
<table>
<thead><tr><th>Section</th><th>Page</th><th>What it covers</th></tr></thead>
<tbody>
<tr><th scope="rowgroup" rowspan="4">Get started</th><td><a href="features/">Features</a></td><td>Everything Booley does, and why.</td></tr>
<tr><td><a href="setup/">Setup</a></td><td>Connect an installed Booley to your RTL project.</td></tr>
<tr><td><a href="usage/">Usage</a></td><td>Day-to-day work with the Developer Agent and waveforms.</td></tr>
<tr><td><a href="goal-mode/">Goal Mode</a></td><td>Evidence-backed Goals, Goalsets, and review.</td></tr>
<tr><th scope="rowgroup" rowspan="6">Flows</th><td><a href="flow-reference/">Running Flows</a></td><td>What every Flow shares, from choosing Targets to reading results.</td></tr>
<tr><td><a href="flow-sim/">Simulation</a></td><td>Build and run a Target's tests and report whether each one passed.</td></tr>
<tr><td><a href="coverage/">Simulation coverage</a></td><td>Measure how much of the RTL your tests exercise, and find what they miss.</td></tr>
<tr><td><a href="flow-lint/">Lint</a></td><td>Check RTL for structural, semantic, and style problems.</td></tr>
<tr><td><a href="flow-synth/">ASIC synthesis</a></td><td>Estimate ASIC area and timing quickly while you iterate on RTL.</td></tr>
<tr><td><a href="flow-fpga/">FPGA implementation</a></td><td>Implement the design for an FPGA and report resources and timing.</td></tr>
<tr><th scope="rowgroup" rowspan="6">Reference</th><td><a href="config/">Config</a></td><td>Project-wide <code>booley.toml</code> settings, plus a table of which page documents every other setting.</td></tr>
<tr><td><a href="targets/">Targets and tests</a></td><td>Describe how your design builds and which tests verify it.</td></tr>
<tr><td><a href="sandbox/">Sandbox</a></td><td>Container policy, egress, custom images, and commercial EDA tools.</td></tr>
<tr><td><a href="stealth/">Stealth Mode</a></td><td>Keep Booley out of your Git history.</td></tr>
<tr><td><a href="cli-reference/">CLI reference</a></td><td>Every command, Flow, and Specialist.</td></tr>
<tr><td><a href="supported-eda-tools/">Supported EDA tools</a></td><td>Which tools each Flow drives, and where they come from.</td></tr>
<tr><th scope="rowgroup" rowspan="3">Help</th><td><a href="doctor/">Doctor</a></td><td>Health checks for Booley's build and execution machinery.</td></tr>
<tr><td><a href="troubleshooting/">Troubleshooting</a></td><td>Symptoms, causes, and fixes.</td></tr>
<tr><td><a href="windows/">Windows</a></td><td>Windows-only first-run traps.</td></tr>
</tbody>
</table>

These pages track the `main` branch, so they can describe changes newer than
your installed release; the [changelog](https://github.com/boldaxolotl/booley/blob/main/CHANGELOG.md)
says what each release contains. The source is [`docs/user/`](https://github.com/boldaxolotl/booley/tree/main/docs/user) on GitHub.
