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
  <div id="beacon" aria-hidden="true"></div>
  <div id="status" role="status">Fetching SPHEREx frames</div>
  <div id="prog-wrap" aria-hidden="true"><div id="prog-bar"></div></div>
  <div id="loadBox" role="status"><span id="loadTxt"></span><button type="button" id="retryBtn">Retry</button></div>
  <div id="veil"></div>

  <section id="land">
    <!-- stage 1: the full-screen hero -->
    <div class="lhero">
      <p class="brand">Draconid Drift</p>
      <h1>26 years of sky, <br>side by side.</h1>
      <p class="sub">See how stars, comets and asteroids have moved since 2000, in NASA SPHEREx's 102 infrared colours.</p>
      <div class="cta"><button id="exploreBtn" type="button">Explore the sky</button><button id="demoBtn" type="button">Watch the demo</button></div>
    </div>
    <dl class="lcol">
      <div><dt>Instrument</dt><dd>NASA SPHEREx</dd></div>
      <div><dt>Wavelengths</dt><dd>0.75 – 5.0 µm, 102 bands</dd></div>
      <div><dt>Baseline</dt><dd>2MASS, 2000 · WISE, 2010</dd></div>
      <div><dt>Featured</dt><dd>Barnard's Star<br>WISE 0855-0714<br>Epsilon Indi B<br>R Draconis</dd></div>
    </dl>
    <!-- stage 2: the globe on the right, the list and the data on the left -->
    <div class="lside">
      <section class="lbox lquick" aria-label="Start here">
        <button type="button" data-key="barnard"><b>See a star move</b><small>Barnard's Star</small></button>
        <button type="button" data-key="rdra"><b>See brightness change</b><small>R Draconis</small></button>
        <button type="button" data-key="wise0855"><b>Explore a cold brown dwarf</b><small>WISE 0855</small></button>
      </section>
      <section class="lbox lpick" aria-label="Find a star">
        <label class="lsearch"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.2"/><path d="M15.2 15.2 20 20"/></svg><input id="lq" type="text" placeholder="Search a star, galaxy, or coordinates…" autocomplete="off" spellcheck="false" aria-label="Search a star, galaxy, or coordinates"></label>
        <form id="cform" class="cform" autocomplete="off">
          <input id="cra" type="text" placeholder="RA (269.45)" spellcheck="false" aria-label="Right ascension"><input id="cdec" type="text" placeholder="Dec (4.69)" spellcheck="false" aria-label="Declination">
          <select id="csize" aria-label="Area around the coordinates"><option value="5">5′</option><option value="15" selected>15′</option><option value="30">30′</option><option value="60">1°</option><option value="180">3°</option></select>
          <button type="submit">Go</button>
          <p id="cerr" role="alert"></p>
        </form>
        <ul id="startList"></ul>
      </section>
      <section class="lbox ldata" aria-label="What you are looking at">
        <h2>You are looking at</h2>
        <dl id="liveView">
          <div><dt>Pointer</dt><dd id="lvPos">—</dd></div>
          <div><dt>Field</dt><dd id="lvFov">—</dd></div>
          <div><dt>Galactic</dt><dd id="lvGal">—</dd></div>
          <div><dt>Region</dt><dd id="lvWhere">—</dd></div>
          <div><dt>Map</dt><dd id="lvMap">—</dd></div>
        </dl>
      </section>
    </div>
    <div class="lctrl" role="group" aria-label="Map controls">
      <div class="wl" role="radiogroup" aria-label="Wavelength of the map">
        <button type="button" data-map="dss" role="radio" aria-checked="false">Visible<small>0.4–0.7 µm</small></button>
        <button type="button" data-map="tmass" role="radio" aria-checked="true" class="on">Near-infrared<small>1.2–2.2 µm</small></button>
        <button type="button" data-map="wise" role="radio" aria-checked="false">Mid-infrared<small>3.4–12 µm</small></button>
      </div>
      <div class="zm"><button type="button" id="zOut" aria-label="Zoom out">−</button><button type="button" id="zIn" aria-label="Zoom in">+</button><button type="button" id="zHome">Whole sky</button></div>
    </div>
  </section>

  <header id="bar">
    <button id="brand" type="button"><img src="/icons/favicon.svg" alt="" width="28" height="28">Draconid Drift</button>
    <nav>
      <button class="navb" data-page="discoveries" type="button">Discoveries</button>
      <button class="navb" data-page="compare" type="button">Compare</button>
      <button class="navb" data-page="how" type="button">How it works</button>
      <button class="navb" data-page="data" type="button">Data</button>
      <div id="search">
        <div id="sfield"><input id="q" type="text" placeholder="Search a star, galaxy, or coordinates…" autocomplete="off" spellcheck="false" aria-label="Search a star, galaxy, or coordinates"></div>
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
      <div class="speed" role="group" aria-label="Playback">
        <button type="button" id="playBtn" aria-label="Pause" title="Pause to inspect a frame">❚❚</button>
        <input id="frameSl" type="range" min="0" max="1" step="1" value="0" aria-label="Observation date">
        <span id="frameDate"></span>
        <button type="button" data-s="0.5">0.5×</button><button type="button" data-s="1" class="on">1×</button><button type="button" data-s="2">2×</button>
      </div></section>
    <section class="panel" id="pDepth" aria-label="3D map of this patch of sky: distance and time">
      <canvas id="depthCv"></canvas><div id="depthTags" aria-hidden="true"></div><canvas id="rulerCv" aria-hidden="true"></canvas>
      <div class="lab"><h2>Distance &amp; time</h2><p id="depthCap"></p><p id="tLab"></p></div>
      <div id="depthTip" role="status"></div>
      <div id="depthWait">Placing every star and galaxy at its distance…</div>
    </section>
    <aside class="panel" id="info">
      <div class="head"><h1 class="big" id="iName"></h1><p class="kind" id="iKind"></p></div>
            <div class="facts" id="iFacts"></div>
      <p class="coord" id="iCoord"></p>
      <div class="blk"><h3 id="specH">Spectrum</h3><div class="spec" id="iSpec"></div></div>
      <div class="blk"><h3>Brightness over time</h3><div class="lc" id="iLc"></div></div>
      <button id="timeBtn" type="button" title="Every star and galaxy in this patch placed at its real distance, moved back and forward in time">3D map: distance &amp; time</button>
      <div class="blk grow"><h3>In the frame</h3><ul class="objs" id="iObjs"></ul></div>
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
        <div class="pg paper" data-page="how">
      <h1>How Draconid Drift works</h1>
      <p class="lede">The data, the methods, and where each method stops working.</p>
      <nav class="toc"><a href="#h-data">1 Data</a><a href="#h-cube">2 Exposures to a cube</a><a href="#h-motion">3 Motion</a><a href="#h-search">4 Finding unknown movers</a><a href="#h-spec">5 Spectra</a><a href="#h-lc">6 Brightness over time</a><a href="#h-frame">7 What is in the frame</a><a href="#h-3d">8 Distances and the 3D map</a><a href="#h-lim">9 Limits</a><a href="#h-ref">References</a></nav>

      <h2 id="h-data">1 Data</h2>
      <table class="dt">
        <thead><tr><th>Survey</th><th>Epoch</th><th>Wavelengths</th><th>Pixel</th><th>Used for</th></tr></thead><tbody>
        <tr><td>SPHEREx</td><td>2025–26</td><td>0.75–5.0 µm, 102 channels</td><td>6.15″</td><td>the images, spectra, brightness, motion</td></tr>
        <tr><td>2MASS</td><td>1997–2001</td><td>J 1.25, H 1.65, Ks 2.17 µm</td><td>1″ (2″ native)</td><td>the sky of 2000</td></tr>
        <tr><td>AllWISE</td><td>2010</td><td>3.4, 4.6, 12, 22 µm</td><td>1.375″</td><td>comparison beyond 2.4 µm</td></tr>
        <tr><td>NEOWISE</td><td>2014–24</td><td>3.4, 4.6 µm</td><td>—</td><td>brightness of saturated stars</td></tr>
        <tr><td>Gaia DR3</td><td>2016.0</td><td>optical</td><td>—</td><td>parallax, proper motion, radial velocity</td></tr>
        <tr><td>SIMBAD</td><td>—</td><td>—</td><td>—</td><td>names, types, spectral classes, redshifts</td></tr>
      </tbody></table>
      <p>SPHEREx observes through linear variable filters: the wavelength a pixel sees changes across each of its six detectors. So each exposure of a given spot is taken at a different wavelength, and over a six-month survey the exposures cover its whole spectrum. Two frames of the same spot are therefore never at the same colour.</p>

      <h2 id="h-cube">2 From exposures to a cube</h2>
      <figure><svg viewBox="0 0 680 120" class="fig"><g font-size="12" fill="#e9edf2" text-anchor="middle">
        <rect x="4" y="34" width="110" height="52" rx="4" class="bx"/><text x="59" y="56">SPHEREx archive</text><text x="59" y="72" class="mut">S3, byte ranges</text>
        <rect x="140" y="34" width="110" height="52" rx="4" class="bx"/><text x="195" y="56">Mask &amp; subtract</text><text x="195" y="72" class="mut">flags, zodiacal light</text>
        <rect x="276" y="34" width="110" height="52" rx="4" class="bx"/><text x="331" y="56">Reproject</text><text x="331" y="72" class="mut">north-up TAN grid</text>
        <rect x="412" y="34" width="110" height="52" rx="4" class="bx"/><text x="467" y="56">Flatten</text><text x="467" y="72" class="mut">plane, σ-clipped</text>
        <rect x="548" y="34" width="128" height="52" rx="4" class="bx hi"/><text x="612" y="56">Cube C(t, x, y)</text><text x="612" y="72" class="mut">+ variance, λ(t)</text>
        <g stroke="#b98089" stroke-width="1.5"><path d="M114 60h22M250 60h22M386 60h22M522 60h22"/></g></g></svg>
        <figcaption>Figure 1. Every exposure that covers the chosen spot goes through the same four steps.</figcaption></figure>
      <p>For a field of side \(L\) centred on \((\alpha_0,\delta_0)\), every SPHEREx exposure whose footprint covers it is found in a spatial index and only the needed bytes are read. Flagged pixels (saturation, cosmic rays, bad pixels) are masked, the zodiacal-light model is subtracted, and the cut-out is resampled onto a common north-up tangent-plane grid with pixel \(p = 6.15''\). Fields wider than 0.5° keep about 361 pixels across, so \(p = L/361\).</p>
      <p>Each frame still carries a smooth background left over from the zodiacal subtraction. We fit a tilted plane \(b(x,y)=c_0+c_1x+c_2y\) by least squares, clip pixels more than \(3\sigma\) from it (stars, movers), refit, and subtract. The result is a cube of images \(C_i(x,y)\) with variances \(V_i(x,y)\), observation times \(t_i\) and wavelengths \(\lambda_i\). Frames closer than a few days apart are grouped into one <em>pass</em>.</p>
      <p>The noise-weighted <b>deep stack</b>, used wherever a clean, deep picture is needed, is</p>
      $$ S(x,y)=\frac&#123;\sum_i w_i(x,y)\,C_i(x,y)&#125;&#123;\sum_i w_i(x,y)&#125;,\qquad w_i=\frac&#123;1&#125;&#123;V_i&#125;,\qquad \operatorname&#123;Var&#125;S=\frac&#123;1&#125;&#123;\sum_i w_i&#125;. $$

      <h2 id="h-motion">3 Motion of known stars</h2>
      <figure><svg viewBox="0 0 680 170" class="fig"><g font-size="12" fill="#e9edf2">
        <circle cx="170" cy="110" r="16" fill="none" stroke="#9aa0a8" stroke-width="1.5"/><text x="140" y="148">2000 (2MASS)</text>
        <circle cx="500" cy="52" r="16" fill="none" stroke="#b98089" stroke-width="1.5"/><text x="470" y="26">2026 (SPHEREx)</text>
        <path d="M186 103 L484 59" stroke="#e8be8c" stroke-width="1.5" stroke-dasharray="5 5"/>
        <path d="M484 59 l-12 -1 l5 9z" fill="#e8be8c"/>
        <text x="300" y="70" fill="#e8be8c">Δθ = μ Δt</text>
        <path d="M170 110 h120" stroke="#5c6470"/><path d="M170 110 v-80" stroke="#5c6470"/><text x="294" y="114" class="mut">east</text><text x="160" y="24" class="mut">north</text></g></svg>
        <figcaption>Figure 2. A star with proper motion \(\mu\) moves \(\mu\,\Delta t\) across the sky between two epochs. The rings on the page mark its two positions.</figcaption></figure>
      <p>A catalogue position \((\alpha_0,\delta_0)\) at epoch \(t_0\) with proper motion \((\mu_&#123;\alpha*&#125;,\mu_\delta)\) (where \(\mu_&#123;\alpha*&#125;=\mu_\alpha\cos\delta\)) is carried to time \(t\) by</p>
      $$ \delta(t)=\delta_0+\mu_\delta\,(t-t_0),\qquad \alpha(t)=\alpha_0+\frac&#123;\mu_&#123;\alpha*&#125;\,(t-t_0)&#125;&#123;\cos\delta(t)&#125;. $$
      <p>The shift in pixels is \(\Delta=\mu\,\Delta t/p\). For Barnard's Star, \(\mu=10.4''\,\mathrm&#123;yr^&#123;-1&#125;&#125;\): over the 26 years between 2MASS and SPHEREx it moves \(270''\), about 44 SPHEREx pixels, which is why it visibly jumps between the two panels.</p>
      <p>A star is marked as moved only after a check. Each Gaia DR3 star (SIMBAD as back-up) that should shift by a few pixels is carried to the SPHEREx epoch and kept only if the deep stack shows a point source there, centred within one pixel, while its old position is much fainter. Objects Gaia cannot see, such as cold brown dwarfs, get the same test using their catalogue motion.</p>

      <h2 id="h-search">4 Finding movers nobody listed</h2>
      <p>To look for objects with no catalogue entry, the frames are shifted along a trial velocity \(\mathbf v=(v_x,v_y)\) in pixels per year and stacked (shift-and-stack):</p>
      $$ S_&#123;\mathbf v&#125;(\mathbf x)=\frac&#123;\sum_i w_i\,C_i\big(\mathbf x+\mathbf v\,(t_i-\bar t)\big)&#125;&#123;\sum_i w_i&#125;,\qquad \nu_&#123;\mathbf v&#125;(\mathbf x)=\frac&#123;S_&#123;\mathbf v&#125;(\mathbf x)&#125;&#123;\sigma_&#123;\mathbf v&#125;&#125;. $$
      <p>A real mover stacks into a sharp peak only at its true velocity. Before stacking, each frame has subtracted from it a co-add of frames from <em>other</em> passes taken at nearly the same wavelength (within a fraction of \(\lambda\)), which removes static sources without mistaking a star's colour for change. The noise \(\sigma_&#123;\mathbf v&#125;\) is measured from the lower half of the stack's pixel distribution only, because smeared stars inflate the upper half.</p>
      <p>The test statistic compares moving against still, \(T=\nu_&#123;\mathbf v&#125;^2-\nu_0^2\). Its significance is measured empirically: the frame times are shuffled \(N\) times, the whole search over the same region and velocity grid is rerun on each shuffle, and</p>
      $$ \mathrm&#123;FAP&#125;=\frac&#123;1+\#\&#123;T_&#123;\text&#123;shuffled&#125;&#125;\ge T\&#125;&#125;&#123;1+N&#125;,\qquad \text&#123;confidence&#125;=1-\mathrm&#123;FAP&#125;. $$
      <p>Each shuffle repeats the full search, so the look-elsewhere effect is included. Confidence is not the probability that an object is real. Five further checks cover that:</p><ul><li>enough signal</li><li>motion fits better than no motion</li><li>both halves of the data agree</li><li>the result survives dropping any one frame</li><li>the pixels along the track are clean</li></ul>

      <h2 id="h-spec">5 Spectra</h2>
      <p>For a source at pixel \((x_i,y_i)\) in frame \(i\) (moving sources follow their track), aperture photometry inside radius \(r\) with a local background \(b_i\) from a surrounding annulus gives</p>
      $$ F_i=\Omega_&#123;\text&#123;pix&#125;&#125;\sum_&#123;\text&#123;aperture&#125;&#125;\big(C_i-b_i\big),\qquad \Omega_&#123;\text&#123;pix&#125;&#125;=\Big(\frac&#123;p&#125;&#123;206\,264.8''&#125;\Big)^2\ \text&#123;sr&#125;, $$
      <p>so with \(C\) in MJy sr\(^&#123;-1&#125;\), \(F_i\,[\mu\mathrm&#123;Jy&#125;]=10^&#123;12&#125;\,\Omega_&#123;\text&#123;pix&#125;&#125;\sum(C_i-b_i)\). Each frame contributes one point \((\lambda_i,F_i)\); with all six detectors loaded that is about a hundred points between 0.75 and 5 µm, binned in log-spaced wavelength bins and shown with their median.</p>
      <figure><svg viewBox="0 0 680 150" class="fig"><g font-size="12" fill="#e9edf2">
        <path d="M30 120 H650" stroke="#5c6470"/><text x="640" y="140" class="mut">λ</text>
        <path d="M40 60 C140 50 200 52 250 58 S300 60 310 66 L330 104 L350 70 S420 74 520 78 S600 84 640 86" fill="none" stroke="#e8be8c" stroke-width="2"/>
        <path d="M250 58 L410 72" stroke="#b98089" stroke-dasharray="4 4"/>
        <path d="M330 66 V104" stroke="#9aa0a8"/><text x="338" y="96">depth d</text>
        <rect x="236" y="40" width="28" height="80" fill="rgba(185,128,137,.12)"/><rect x="396" y="40" width="28" height="80" fill="rgba(185,128,137,.12)"/>
        <text x="214" y="34" class="mut">side window</text><text x="380" y="34" class="mut">side window</text></g></svg>
        <figcaption>Figure 3. A feature is labelled only when the dip below the straight line between its two side windows is deep and significant.</figcaption></figure>
      <p>Molecular bands (water at 1.4 and 1.9 µm, CO at 2.3 and 4.7 µm, water ice at 3.05 µm, CO\(_2\) at 4.27 µm, methane at 3.3 µm) are labelled only when measured. The continuum \(F_c(\lambda)\) is the straight line between median fluxes in two side windows, and a feature is reported when</p>
      $$ d=1-\frac&#123;F(\lambda_c)&#125;&#123;F_c(\lambda_c)&#125;\ \ge\ 0.08\qquad\text&#123;and&#125;\qquad \frac&#123;d&#125;&#123;\sigma_d&#125;\ \ge\ 3. $$
      <p>When a star saturates SPHEREx its core is masked; the spectrum's shape is then measured in a ring around the core, whose absolute level is lower than the true total but whose shape survives.</p>

      <h2 id="h-lc">6 Brightness over time</h2>
      <p>Because each exposure sees a different wavelength, raw fluxes vary with the star's colour even if it is perfectly steady. We first remove the colour. With \(u_i=\ln\lambda_i\) and \(f_i=\ln F_i\), the colour model \(m(u)\) is a running median of \(f\) along wavelength that leaves each point out of its own window (or a low-order polynomial when there are fewer than 15 points), and</p>
      $$ r_i=\exp\!\big(f_i-m(u_i)\big),\qquad s=1.4826\,\operatorname&#123;median&#125;|r_i-1|. $$
      <p>Variability is claimed only if \(s\) exceeds \(\max(3\sigma_&#123;\text&#123;noise&#125;&#125;,0.08)\) <em>and</em> the changes are coherent in time, measured by the lag-one autocorrelation of the time-ordered \(r_i\):</p>
      $$ \rho_1=\frac&#123;\sum_i (r_i-\bar r)(r_&#123;i+1&#125;-\bar r)&#125;&#123;\sum_i (r_i-\bar r)^2&#125;>0.3. $$
      <p>Scatter that jumps from one exposure to the next (glare from a neighbour, a poor background) fails this test and is reported as noise. For stars SPHEREx saturates on, the same question is put to NEOWISE: its single-exposure 3.4 µm magnitudes are matched to where the star actually was at each moment, grouped into visits (exposures within 5 days), and compared as \(r_k=10^&#123;-0.4\,(m_k-\tilde m)&#125;\).</p>

      <h2 id="h-frame">7 What is in the frame</h2>
      <p>Sources are local maxima of the deep stack more than \(6\sigma\) above the sky, after removing the false peaks a bright star makes in its own halo and diffraction spikes. Each is matched to SIMBAD within \(6''\) (or half the object's catalogued size). Unnamed sources are classified by a multinomial logistic regression trained on SPHEREx sources that SIMBAD has already identified, using features measured on the SPHEREx frames themselves: concentration, size beyond the PSF, ellipticity, colour slope \(d\ln F/d\ln\lambda\), variability and signal-to-noise, with their pairwise products. For feature vector \(\mathbf z\) the class probabilities are</p>
      $$ P(k\mid\mathbf z)=\frac&#123;e^&#123;\mathbf w_k\cdot\mathbf z&#125;&#125;&#123;\sum_j e^&#123;\mathbf w_j\cdot\mathbf z&#125;&#125;,\qquad k\in\&#123;\text&#123;star&#125;,\ \text&#123;galaxy&#125;,\ \text&#123;nebula&#125;\&#125;, $$
      <p>and the page marks every such label as the model's guess.</p>

      <h2 id="h-3d">8 Distances and the 3D map</h2>
      <p>Stars are placed by Gaia DR3 parallax \(\varpi\), keeping only \(\varpi/\sigma_\varpi>5\) (distance good to about 20%):</p>
      $$ d\,[\mathrm&#123;pc&#125;]=\frac&#123;1&#125;&#123;\varpi\,[\mathrm&#123;arcsec&#125;]&#125;,\qquad d\,[\mathrm&#123;ly&#125;]=3.2616\,d\,[\mathrm&#123;pc&#125;]. $$
      <p>Galaxies are placed by redshift, as the light-travel (lookback) time in the Planck 2018 cosmology,</p>
      $$ t_L(z)=\int_0^z\frac&#123;dz'&#125;&#123;(1+z')\,H(z')&#125;,\qquad H(z)=H_0\sqrt&#123;\Omega_m(1+z)^3+\Omega_\Lambda&#125;, $$
      <p>with \(H_0=67.7\ \mathrm&#123;km\,s^&#123;-1&#125;\,Mpc^&#123;-1&#125;&#125;\), \(\Omega_m=0.31\); nearby galaxies use SIMBAD's measured distances (Cepheids, tip of the red giant branch) instead.</p>
      <figure><svg viewBox="0 0 680 160" class="fig"><g font-size="12" fill="#e9edf2">
        <path d="M40 80 L640 18 M40 80 L640 142" stroke="#5a3a40" stroke-width="1.2"/>
        <path d="M40 80 H650" stroke="#9aa0a8"/><circle cx="40" cy="80" r="6" fill="#5b8fd9"/><text x="22" y="104">Earth</text>
        <g class="mut" text-anchor="middle"><path d="M160 76v8M280 76v8M400 76v8M520 76v8" stroke="#9aa0a8"/><text x="160" y="100">10 ly</text><text x="280" y="100">1,000 ly</text><text x="400" y="100">100,000 ly</text><text x="520" y="100">10 million ly</text></g>
        <circle cx="190" cy="70" r="3" fill="#ffe6c8"/><circle cx="330" cy="92" r="3" fill="#ffe6c8"/><ellipse cx="560" cy="66" rx="10" ry="4" fill="#d8c4ff"/></g></svg>
        <figcaption>Figure 4. The patch of sky as a pyramid from Earth, with distance on a logarithmic axis so a nearby star and a distant galaxy fit in one view.</figcaption></figure>
      <p>Distance is drawn on a logarithmic axis, \(s=\log_&#123;10&#125;d\). Moving in time uses straight-line motion in space. A star's tangential velocity follows from its proper motion and distance,</p>
      $$ v_t\,[\mathrm&#123;km\,s^&#123;-1&#125;&#125;]=4.74\,\mu\,[''\,\mathrm&#123;yr^&#123;-1&#125;&#125;]\;d\,[\mathrm&#123;pc&#125;], $$
      <p>and with its radial velocity \(v_r\) (when Gaia measured one) its position after \(t\) years is \(\mathbf r(t)=\mathbf r_0+\mathbf v\,t\). The new distance is \(|\mathbf r(t)|\) and the new direction is the angle of \(\mathbf r(t)\); the tag next to each star shows the distance change \(|\mathbf r(t)|-|\mathbf r_0|\). Beyond \(10^5\) years a straight line stops being meaningful (stars orbit the Galaxy), so motion is frozen there; further back the slider instead highlights objects whose light left at that time.</p>

      <h2 id="h-lim">9 Limits</h2>
      <ul>
        <li>Within one six-month survey a pass covers each spot for a few days, so motions slower than about one pixel per year are not measurable from SPHEREx alone; the archive comparison stretches the baseline to 26 years.</li>
        <li>Very bright stars saturate; their spectra come from the wings and their brightness from NEOWISE, which itself shows a slow calibration drift for the brightest stars.</li>
        <li>Unnamed-source classes are a model's guess, and the 3D map shows only objects with a measured distance.</li>
        <li>Without the EC2 data server, at most 24 exposures are read per detector (evenly spread in time), and wide fields use coarser pixels.</li>
      </ul>

      <h2 id="h-ref">References</h2>
      <ol class="refs">
        <li>Doré, O. et al. 2014, <i>Cosmology with the SPHEREX All-Sky Spectral Survey</i>, arXiv:1412.4872.</li>
        <li>Skrutskie, M. F. et al. 2006, <i>The Two Micron All Sky Survey (2MASS)</i>, AJ 131, 1163.</li>
        <li>Wright, E. L. et al. 2010, <i>The Wide-field Infrared Survey Explorer (WISE)</i>, AJ 140, 1868; Cutri, R. M. et al. 2013, AllWISE Explanatory Supplement.</li>
        <li>Mainzer, A. et al. 2014, <i>Initial Performance of the NEOWISE Reactivation Mission</i>, ApJ 792, 30.</li>
        <li>Gaia Collaboration, Vallenari, A. et al. 2023, <i>Gaia Data Release 3</i>, A&amp;A 674, A1.</li>
        <li>Wenger, M. et al. 2000, <i>The SIMBAD astronomical database</i>, A&amp;AS 143, 9.</li>
        <li>Planck Collaboration 2020, <i>Planck 2018 results VI: Cosmological parameters</i>, A&amp;A 641, A6.</li>
      </ol>
    </div>
    <div class="pg wide" data-page="discoveries"><h1>Discoveries</h1>
      <p>Stars the pipeline confirmed moving, and objects whose brightness changes. Click one to fly there.</p>
      <div class="dhead"><span>Object</span><span>What we found</span><span>Confidence</span><span>Distance</span><span>Logged</span></div>
      <ul id="dlist" class="dlist"></ul></div>
    <div class="pg wide" data-page="compare"><h1>Compare</h1>
      <p>Search any two objects and see them side by side.</p>
      <div class="cpick">
        <div class="cbox"><span>First</span><input id="cA" type="text" autocomplete="off" spellcheck="false" placeholder="Search any star or galaxy"><ul class="csug" id="cAs"></ul></div>
        <div class="cbox"><span>Second</span><input id="cB" type="text" autocomplete="off" spellcheck="false" placeholder="Search any star or galaxy"><ul class="csug" id="cBs"></ul></div>
      </div>
      <div id="ctable" class="ctable"></div>
      <h2 class="csub">Spectra</h2>
      <div id="cspec" class="cspec"></div></div>
    <div class="pg" data-page="data"><h1>Data</h1>
      <p><b>SPHEREx</b> spectral images, NASA/IPAC Infrared Science Archive.</p>
      <p><b>2MASS</b> and <b>AllWISE</b> sky maps, served as HiPS by IRSA and CDS.</p>
      <p><b>Gaia DR3</b> parallaxes, proper motions and radial velocities, ESA.</p>
      <p><b>NEOWISE</b> single-exposure photometry 2014–2024, NASA/IPAC Infrared Science Archive.</p>
      <p><b>SIMBAD</b> object names and types, CDS Strasbourg (Harvard mirror).</p>
</div>
  </section>
  <div id="toast" role="status"></div>
  <div id="tour" role="dialog" aria-live="polite"><p id="tourTx"></p><i id="tourBar"></i><div class="tb"><span id="tourN"></span><button id="tourSkip" type="button">End tour</button><button id="tourNext" type="button">Next</button></div></div>
  <div id="tourBlock" aria-hidden="true"></div>
  <div id="tourCur" aria-hidden="true"></div>
  <div id="tourHl" aria-hidden="true"></div>
  <div id="demo" aria-hidden="true"><video id="demoV" src="/demo.mp4" playsinline preload="auto"></video>
    <button id="demoSkip" type="button">Skip</button></div>
</div>
