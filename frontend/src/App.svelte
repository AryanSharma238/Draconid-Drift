<script>
  import { onMount } from "svelte";
  import { mountStage } from "./lib/stageEngine.js";

  onMount(() => {
    mountStage();
  });
</script>

<div id="app" data-view="home">
  <!-- the sky: one Aladin map, clipped and scaled by CSS when it squishes into the left panel -->
  <div id="sky-clip"><div id="sky-inner"><div id="sky"></div></div></div>
  <div id="blend"></div>
  <div id="sel" aria-hidden="true"></div>
  <div id="tip" aria-hidden="true">Right-click and drag to select an area</div>
  <div id="beacon" aria-hidden="true">
    <div class="lock-ring"></div>
    <div class="lock-dot"></div>
    <span class="lock-label"></span>
  </div>
  <div id="status" role="status">Fetching SPHEREx frames</div>
  <div id="prog-wrap" aria-hidden="true"><div id="prog-bar"></div></div>
  <div id="veil"></div>

  <section id="land">
    <h1>26 years of sky, side by side.</h1>
    <p class="brand">Draconid Drift</p>
    <p class="sub">NASA SPHEREx, compared with the sky of 2000.</p>
    <div class="cta"><button id="demoBtn" type="button">Demo</button><button id="exploreBtn" type="button">Explore</button></div>
  </section>

  <header id="bar">
    <button id="brand" type="button">Draconid Drift</button>
    <nav>
      <button class="navb" data-page="challenge" type="button">Challenge</button>
      <button class="navb" data-page="how" type="button">How it works</button>
      <button class="navb" data-page="data" type="button">Data</button>
      <div id="search">
        <div id="sfield"><input id="q" type="text" placeholder="Search for an object" autocomplete="off" spellcheck="false" aria-label="Search for an object"></div>
        <button id="sbtn" type="button" aria-label="Search" aria-expanded="false">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
            <circle class="lens" cx="10.5" cy="10.5" r="6.2"/><path class="handle" d="M15.2 15.2 20 20"/><path class="x1" d="M6 6 18 18"/><path class="x2" d="M18 6 6 18"/>
          </svg>
        </button>
        <ul id="results" role="listbox"></ul>
      </div>
    </nav>
  </header>

  <main id="page2">
    <section class="panel img" id="pWise"><canvas id="wiseCv"></canvas><canvas class="hl" id="wiseHl"></canvas>
      <div class="lab"><h2 id="wiseH">AllWISE</h2><p id="wiseCap">2010</p></div></section>
    <section class="panel img" id="pSpx"><canvas id="spxCv"></canvas><canvas class="hl" id="spxHl"></canvas>
      <div class="lab"><h2>SPHEREx</h2><p id="spxCap"></p></div>
      <button id="cmpBtn" type="button" aria-pressed="true" aria-label="Hide the comparison image" title="Hide the comparison image">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M12 5v14"/></svg>
      </button>
      <div class="speed" role="group" aria-label="Playback speed">
        <button type="button" data-s="0.5">0.5×</button><button type="button" data-s="1" class="on">1×</button><button type="button" data-s="2">2×</button>
      </div>
      <div class="proc-mode" role="group" aria-label="Processing mode">
        <button id="modeSub" type="button" title="Difference image: Subtract comparison to expose shifting motion">Diff</button>
        <button id="modeStack" type="button" title="Deep temporal sum across all visits">Stack</button>
        <button id="dlBtn" type="button" title="Download comparison and SPHEREx frames">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </button>
        <button id="shareBtn" type="button" title="Share direct link to coordinates">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>
        </button>
      </div></section>
    <section class="panel" id="pDepth" aria-label="3D map of this patch of sky: distance and time">
      <canvas id="depthCv"></canvas><div id="depthTags" aria-hidden="true"></div><canvas id="rulerCv" aria-hidden="true"></canvas>
      <div class="lab"><h2>Distance &amp; time</h2><p id="depthCap"></p><p id="tLab"></p></div>
      <div id="depthTip" role="status"></div>
      <div id="depthWait">Placing every star and galaxy at its distance…</div>
    </section>
    <aside class="panel" id="info">
      <div class="head"><h1 class="big" id="iName"></h1><p class="kind" id="iKind"></p>
        <button id="timeBtn" type="button" title="See this patch in the past and the future (coming next)">Past &amp; future</button></div>
      <div class="facts" id="iFacts"></div>
      <p class="coord" id="iCoord"></p>
      <div class="blk"><h3 id="specH">Spectrum</h3><div class="spec" id="iSpec"></div></div>
      <div class="blk"><h3>Brightness over time</h3><div class="lc" id="iLc"></div></div>
      <div class="blk grow"><h3>In the frame</h3><ul class="objs" id="iObjs"></ul></div>
      <p class="src" id="iSrc"></p>
    </aside>
  </main>

  <footer id="foot">
    <div id="wave" role="slider" tabindex="0" aria-label="Wavelength" aria-valuemin="1" aria-valuemax="6" aria-valuenow="3">
      <div id="waveSegs"></div><div id="waveKnob"></div>
    </div>
    <p id="waveLab"></p>
    <div id="timebar">
      <div class="tw"><input id="tslider" type="range" min="-1000" max="1000" step="1" value="0" aria-label="Time, from 13.8 billion years ago to 100,000 years ahead"><div id="tticks" aria-hidden="true"></div></div>
    </div>
  </footer>

  <section id="about" aria-hidden="true">
    <div class="pg" data-page="challenge"><h1>NASA Space Apps 2026: Planet X &amp; SPHEREx</h1>
      <p>Built for the NASA Space Apps Challenge 2026. SPHEREx maps the whole sky once every six months in 102 bands of near-infrared light (0.75–5.0 µm), making it uniquely sensitive to molecules like water, carbon monoxide, and methane.</p>
      <p><b>5 Benchmark Targets:</b></p>
      <ul class="chal-list">
        <li><b>Barnard’s Star</b> — Fast-moving nearby red dwarf (10.3″/yr) with 26-year baseline shift.</li>
        <li><b>Epsilon Indi B</b> — Unresolved cold brown dwarf pair moving in tandem with Epsilon Indi A.</li>
        <li><b>WISE 0855-0714</b> — Extremely faint, coldest known brown dwarf (RA 08h 55m 10.8s, Dec −07° 14′ 42″).</li>
        <li><b>RW Aurigae</b> — Young, irregular dimming variable star with unresolved binary disk.</li>
        <li><b>R Draconis</b> — Mira pulsating long-period variable star exhibiting massive periodic brightness swings.</li>
      </ul>
      <p><b>Key Capabilities:</b> On-the-fly 102-band spectra generation, 6-detector array stacking, difference subtraction, direct URL coordinate embedding, and multi-mission archival overlay (2MASS &amp; AllWISE).</p>
    </div>
    <div class="pg" data-page="how"><h1>How it works</h1>
      <p>SPHEREx maps the whole sky every six months in 102 colours of infrared light, from 0.75 to 5 µm. Draconid Drift pulls every SPHEREx exposure that covers the spot you choose, cleans and lines them up, and plays them in time order.</p>
      <p>The bar along the bottom switches between SPHEREx's six detectors, each covering one slice of that range. Every exposure measures one colour at each spot, so measuring a star in all of them gives its spectrum: the dips show water, carbon monoxide or ices.</p>
      <p>Press Compare to put the same patch beside SPHEREx as 2MASS saw it around 2000, blurred to SPHEREx's sharpness. Nearby stars visibly move in between.</p>
      <p>The facts come from SIMBAD and Gaia. Bright sources with no name are sorted into star, galaxy or nebula by a small model trained on SPHEREx sources that SIMBAD has already identified, and are marked as a guess.</p></div>
    <div class="pg" data-page="data"><h1>Data</h1>
      <p><b>SPHEREx</b> spectral images, NASA/IPAC Infrared Science Archive.</p>
      <p><b>2MASS</b> and <b>AllWISE</b> sky maps, served as HiPS by IRSA and CDS.</p>
      <p><b>Gaia DR3</b> proper motions, ESA.</p>
      <p><b>SIMBAD</b> object names and types, CDS Strasbourg (Harvard mirror).</p>
      <p>Built for the NASA Space Apps Challenge 2026, Planet X and SPHEREx.</p></div>
  </section>
  <div id="toast" role="status"></div>
  <div id="demo" aria-hidden="true"><video id="demoV" src="demo.mp4" playsinline preload="auto"></video>
    <button id="demoSkip" type="button">Skip</button></div>
</div>

