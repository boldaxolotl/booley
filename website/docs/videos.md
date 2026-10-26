---
title: Videos
document: Video walkthroughs
---

<div class="strip"><span><a class="strip__home" href="./"><b>VIDEO</b>Video walkthroughs</a></span><span>Booley product family &middot; booley-rtl</span></div>
<h1>Video walkthroughs</h1>
<p class="lede">An engineer drives Booley on a demo project, start to finish. I recorded both videos, then replaced my narration with text-to-speech to stay anonymous for now.</p>

<section aria-labelledby="s1">
  <h2 class="sec" id="s1"><span class="n">1</span>Walkthroughs</h2>
  <!-- Text cards, no embedded player or thumbnail: every link opens YouTube
       in a new tab, so this page makes no request to YouTube. Chapter times
       come from the videos' narration. -->
  <div class="vids">
  <article class="vid" aria-labelledby="v1-t">
    <p class="vid__k"><span>Video 1</span><span><span class="sr">Duration </span>12:43</span></p>
    <h3 class="vid__t" id="v1-t">Design Optimization</h3>
    <p class="vid__d">Measure, optimize, and verify an RTL divider.</p>
    <ol class="vid__ch" aria-label="Chapters (each opens YouTube at that point, in a new tab)">
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=22" target="_blank" rel="noopener"><span class="vid__tc">0:22</span>Tour the PicoRV32 divider</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=73" target="_blank" rel="noopener"><span class="vid__tc">1:13</span>Simulate Dhrystone with tracing</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=172" target="_blank" rel="noopener"><span class="vid__tc">2:52</span>Show the divider in the waveform viewer</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=290" target="_blank" rel="noopener"><span class="vid__tc">4:50</span>Measure latency with bwave: 34 cycles</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=371" target="_blank" rel="noopener"><span class="vid__tc">6:11</span>Synthesize a baseline: 3.1 kGE</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=429" target="_blank" rel="noopener"><span class="vid__tc">7:09</span>Choose radix-4 plus early exit</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=555" target="_blank" rel="noopener"><span class="vid__tc">9:15</span>Implement, re-test, and re-measure</a></li>
        <li><a href="https://youtu.be/zHuvU4QJbvE?t=626" target="_blank" rel="noopener"><span class="vid__tc">10:26</span>Results: up to 15× faster, +77% area</a></li>
    </ol>
    <p class="vid__go"><a href="https://youtu.be/zHuvU4QJbvE" target="_blank" rel="noopener">Watch on YouTube<span aria-hidden="true"> ↗</span><span class="sr"> (opens in a new tab)</span></a></p>
  </article>
  <article class="vid" aria-labelledby="v2-t">
    <p class="vid__k"><span>Video 2</span><span><span class="sr">Duration </span>9:40</span></p>
    <h3 class="vid__t" id="v2-t">Finding and Fixing Bugs</h3>
    <p class="vid__d">Waveform debugging and independent RTL review.</p>
    <ol class="vid__ch" aria-label="Chapters (each opens YouTube at that point, in a new tab)">
        <li><a href="https://youtu.be/hsYHHZcx82w?t=47" target="_blank" rel="noopener"><span class="vid__tc">0:47</span>The bug report: a spurious watchdog interrupt</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=72" target="_blank" rel="noopener"><span class="vid__tc">1:12</span>Ask the agent for the root cause</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=135" target="_blank" rel="noopener"><span class="vid__tc">2:15</span>Add a test that exposes the race</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=159" target="_blank" rel="noopener"><span class="vid__tc">2:39</span>Walk through the failing waveform</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=232" target="_blank" rel="noopener"><span class="vid__tc">3:52</span>Fix the RTL</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=332" target="_blank" rel="noopener"><span class="vid__tc">5:32</span>Catch it earlier with the reviewer Specialist</a></li>
        <li><a href="https://youtu.be/hsYHHZcx82w?t=482" target="_blank" rel="noopener"><span class="vid__tc">8:02</span>Rerun the review with a stronger model</a></li>
    </ol>
    <p class="vid__go"><a href="https://youtu.be/hsYHHZcx82w" target="_blank" rel="noopener">Watch on YouTube<span aria-hidden="true"> ↗</span><span class="sr"> (opens in a new tab)</span></a></p>
  </article>
  </div>
</section>

<section aria-labelledby="s2">
  <h2 class="sec" id="s2"><span class="n">2</span>Try it yourself</h2>
  <p class="lede">The <a href="../demos/">evaluation designs</a> are open-source projects already set up for Booley. Clone one and give the agent your own task.</p>
</section>
