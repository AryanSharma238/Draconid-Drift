  <script>
  import { onMount, onDestroy } from "svelte";
  import { Sky, unit, radec, slerp } from "./lib/sky.js";
  import { renderCutout, SURVEYS } from "./lib/healpix.js";
  import {
    KINDS,
    OTYPE,
    FAMOUS,
    WISETONE,
    SPX_PSF,
    WISE_PSF,
    smooth,
    clamp01,
    compareFor,
    propagate,
    fieldDeg,
    sexa,
    sepDeg,
    offsetOf,
    parseCoords,
    parseSesame,
    toPx,
  } from "./lib/constants.js";

  let view = $state("home");
  let shown = $state(false);
  let searching = $state(false);
  let aboutPage = $state(null);
  let dimming = $state(false);
  let smoothready = $state(false);
  let zoomed = $state(false);

  let presets = $state([]);
  let midEpoch = $state(2026.3);
  let stageResult = $state(null);
  let stageError = $state(null);

  let progWidth = $state(0);
  let toastMsg = $state("");
  let toastOn = $state(false);
  let tipOn = $state(false);
  let tipTransform = $state("");
  let selOn = $state(false);
  let selStyle = $state("");

  let searchQuery = $state("");
  let searchItems = $state([]);
  let searchSel = $state(-1);

  let playing = $state(true);
  let spxCap = $state("");
  let fillWidth = $state(0);
  let thumbLeft = $state(0);
  let spxOn = $state(false);

  let hl = $state(null);

  // DOM refs
  let skyEl, clipEl, innerEl, blendEl, progBarEl, pWiseEl, pSpxEl;
  let wiseCvEl, wiseHlEl, spxCvEl, spxHlEl, trackEl, qEl;

  let sky = null;
  let smoothCv = null;
  let overlay = null;
  let skyCanvas = null;
  let wiseCanvas = null;
  let busy = false;
  let spinning = false;
  let smoothId = 0;
  let req = null;
  let last_res = null;

  let progInterval = null;
  let toastTimer = null;

  let P = null;
  let imgs = [];
  let tpos = 0;
  let dragging = false;
  let raf = 0;
  let lastFrameTime = 0;
  const FPS = 5;

  let CATALOG = null;
  let catLoading = null;
  const thumbCache = new Map();
  const thumbQ = [];
  let thumbBusy = false;
  const objThumbQ = [];
  let objBusy = false;
  let qid = 0;
  let deb = 0;

  const tween = (dur, fn) =>
    new Promise((res) => {
      const t0 = performance.now();
      const step = () => {
        const t = clamp01((performance.now() - t0) / dur);
        fn(t);
        t < 1 ? requestAnimationFrame(step) : res();
      };
      requestAnimationFrame(step);
    });

  function toast(msg) {
    toastMsg = msg;
    toastOn = true;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(
      () => {
        toastOn = false;
      },
      msg.startsWith("Something went wrong") ? 15000 : 4200
    );
  }

  function fitBlend() {
    if (!sky || !blendEl) return;
    const W = sky.W,
      H = sky.H;
    const edge = (dx, dy) => {
      let lo = 0,
        hi = Math.hypot(W, H);
      for (let i = 0; i < 22; i++) {
        const m = (lo + hi) / 2;
        sky.pix2world(W / 2 + dx * m, H / 2 + dy * m) ? (lo = m) : (hi = m);
      }
      return lo;
    };
    const w = edge(1, 0) - 0.5,
      h = edge(0, 1) - 0.5;
    const m = `radial-gradient(ellipse ${w}px ${h}px at 50% 50%, transparent calc(100% - 1.5px), #000 100%)`;
    blendEl.style.webkitMaskImage = blendEl.style.maskImage = m;
  }

  async function refreshSmooth() {
    const my = ++smoothId;
    if (smoothCv) smoothCv.classList.remove("on");
    if (view !== "home" || !sky || sky.fov() > 2) return;
    const [r, d] = sky.center(),
      w = Math.round(sky.W / 1.5),
      h = Math.round(sky.H / 1.5);
    try {
      const c = await renderCutout(SURVEYS.tmass, r, d, sky.fov(), w, h, {});
      if (my !== smoothId || view !== "home") return;
      smoothCv.width = w;
      smoothCv.height = h;
      smoothCv.getContext("2d").drawImage(c, 0, 0);
      smoothCv.classList.add("on");
    } catch (e) {}
  }

  function wake() {
    if (spinning) {
      sky.haltSpin();
      spinning = false;
    }
    sky.grid(true);
  }

  const panelA = () => {
    const r = pSpxEl.getBoundingClientRect();
    return r.width ? r.height / r.width : 1.4;
  };
  const SF = () => Math.min(sky.W * 0.4, (0.8 * (sky.H - 20)) / panelA());
  const ENDFOV = () => (req.fieldDeg * sky.W) / (SF() * panelA());

  function makeOverlay() {
    if (overlay) overlay.remove();
    overlay = document.createElement("canvas");
    overlay.id = "fw";
    Object.assign(overlay.style, {
      position: "absolute",
      inset: "0",
      width: "100%",
      height: "100%",
      opacity: "0",
      pointerEvents: "none",
    });
    innerEl.appendChild(overlay);
  }

  async function flyTo(ra, dec, fov, dur) {
    const c0 = sky.center(),
      f0 = sky.fov();
    const u0 = unit(c0[0], c0[1]),
      u1 = unit(ra, dec);
    const fPeak = Math.min(
      sky.homeFov(),
      Math.max(f0, Math.hypot(...offsetOf(c0, [ra, dec])) * 1.5, fov * 2)
    );
    await tween(dur, (t) => {
      const [r, d] = radec(slerp(u0, u1, smooth(t)));
      let f;
      if (t < 0.3) {
        const q = t / 0.3;
        f = f0 + (fPeak - f0) * smooth(q);
      } else {
        const q = (t - 0.3) / 0.7;
        f = fPeak * Math.pow(fov / fPeak, smooth(q));
      }
      sky.set(r, d, Math.min(f, sky.homeFov()));
      if (overlay) overlay.style.opacity = String(smooth(clamp01((t - 0.75) / 0.25)));
    });
    sky.set(ra, dec, fov);
  }

  async function flyHome(ra, dec, fov, dur) {
    const c0 = sky.center(),
      u0 = unit(c0[0], c0[1]),
      u1 = unit(ra, dec),
      f0 = Math.log(sky.fov()),
      f1 = Math.log(fov);
    await tween(dur, (t) => {
      const p = smooth(t),
        [r, d] = radec(slerp(u0, u1, p));
      sky.set(r, d, Math.exp(f0 + (f1 - f0) * p));
    });
    sky.set(ra, dec, fov);
  }

  function startProgressBar() {
    stopProgressBar();
    progWidth = 0;
    const startT = performance.now();
    const duration = 3800;
    progInterval = setInterval(() => {
      const elapsed = performance.now() - startT;
      const t = Math.min(1, elapsed / duration);
      progWidth = Math.min(smooth(t) * 97, 98);
      if (t >= 1) {
        clearInterval(progInterval);
        progInterval = null;
      }
    }, 30);
  }

  function stopProgressBar(complete = false) {
    if (progInterval) {
      clearInterval(progInterval);
      progInterval = null;
    }
    if (complete) {
      progWidth = 100;
      setTimeout(() => {
        progWidth = 0;
      }, 350);
    } else {
      progWidth = 0;
    }
  }

  async function fetchStage(item, nonce) {
    const params = new URLSearchParams({
      key: item.key || "custom",
      ra: String(item.ra),
      dec: String(item.dec),
      fov: String(item.fov || 180),
      pmra: String(item.pmra || 0),
      pmdec: String(item.pmdec || 0),
      epoch: String(item.epoch || 2000),
      name: item.name || "Selected area",
      detector: item.detector || "SPHEREx-D3",
      nonce,
    });
    try {
      const r = await fetch(`/api/stage?${params.toString()}`);
      const data = await r.json();
      if (!req || req.nonce !== nonce) return;
      if (!r.ok || data.error) {
        stageError = data.error || "Failed to fetch SPHEREx frames.";
        stageResult = null;
      } else {
        stageError = null;
        stageResult = data;
      }
      tryReveal();
    } catch (e) {
      if (!req || req.nonce !== nonce) return;
      stageError = `Connection error (${e.message}).`;
      stageResult = null;
      tryReveal();
    }
  }

  async function go(item) {
    if (busy && view !== "view") return;
    closeSearch(true);
    closeAbout();
    if (view === "view") {
      busy = true;
      stopPlayer();
      view = "leaving";
      clipEl.style.transition = "none";
      clipEl.style.opacity = "0";
      clipEl.style.clipPath = "inset(0 0 0 0 round 0px)";
      innerEl.style.transform = "none";
      clipEl.style.visibility = "visible";
      dimming = true;
      await new Promise((r) => setTimeout(r, 30));
      clipEl.style.transition = "opacity .9s ease";
      clipEl.style.opacity = "1";
      shown = false;
      await new Promise((r) => setTimeout(r, 950));
      dimming = false;
      clipEl.style.transition = "";
      if (overlay) {
        const o = overlay;
        overlay = null;
        o.style.transition = "opacity .6s";
        o.style.opacity = "0";
        setTimeout(() => o.remove(), 700);
      }
      sky.grid(true);
    }

    for (const c of [wiseCvEl, spxCvEl]) {
      if (c) c.getContext("2d").clearRect(0, 0, c.width, c.height);
    }
    spxOn = false;
    busy = true;
    stageResult = null;
    stageError = null;

    const my = String(Date.now()) + Math.random().toString(36).slice(2, 6);
    req = Object.assign({}, item, { nonce: my });
    const cmpI = compareFor(item.detector);
    const shift =
      (Math.hypot(item.pmra || 0, item.pmdec || 0) / 1000) * (midEpoch - cmpI.epoch);
    req.pos = propagate(item, cmpI.epoch);
    req.fieldDeg = fieldDeg(Math.max(item.fov || 180, FRAME_FACTOR * shift + 60));

    // Direct REST call to AWS-backed FastAPI stage bridge (replaces Streamlit iframe postMessage)
    fetchStage(req, my);

    view = "flying";
    sky.haltSpin();
    spinning = false;
    smoothId++;
    if (smoothCv) smoothCv.classList.remove("on");
    wiseCanvas = null;
    skyCanvas = null;
    makeOverlay();
    smoothready = false;

    const cw = 768,
      ch = Math.round((cw * sky.H) / sky.W);
    renderCutout(SURVEYS.tmass8, req.pos[0], req.pos[1], ENDFOV(), cw, ch, {})
      .then((c) => {
        if (!req || req.nonce !== my) return;
        skyCanvas = c;
        overlay.width = c.width;
        overlay.height = c.height;
        overlay.getContext("2d").drawImage(c, 0, 0);
        smoothready = true;
        if (req.arrived) overlay.style.opacity = "1";
      })
      .catch(() => {});

    const f = pSpxEl.getBoundingClientRect(),
      pw = Math.min(560, Math.round(f.width * Math.min(2, devicePixelRatio || 1))),
      ph = Math.round((pw * f.height) / f.width);
    req.wiseJob = { survey: cmpI.survey, pw, ph };
    req.wiseJob.p = renderCutout(
      req.wiseJob.survey,
      req.pos[0],
      req.pos[1],
      (req.fieldDeg * pw) / ph,
      pw,
      ph,
      WISETONE
    ).catch(() => null);

    startProgressBar();
    await flyTo(req.pos[0], req.pos[1], ENDFOV(), 3000);
    req.arrived = true;
    if (skyCanvas) overlay.style.opacity = "1";
    view = "waiting";
    tryReveal();
  }

  const geom = () => {
    const W = sky.W,
      H = sky.H,
      f = pSpxEl.getBoundingClientRect(),
      S = SF();
    return {
      W,
      H,
      f,
      S,
      k: f.width / S,
      A: f.height / f.width,
      dx: f.left + f.width / 2 - W / 2,
      dy: f.top + f.height / 2 - H / 2,
    };
  };

  async function tryReveal() {
    const res = stageResult;
    if (!req || view === "flying" || view === "view" || view === "settling") return;
    if (stageError) {
      stopProgressBar(false);
      toast(stageError);
      return abortHome();
    }
    if (!res || res.nonce !== req.nonce) return;

    stopProgressBar(true);
    view = "settling";
    last_res = res;
    loadFrames(res);

    const wisePromise = (async () => {
      try {
        const j = req.wiseJob,
          same =
            Math.abs(res.field_deg / req.fieldDeg - 1) < 0.02 &&
            Math.hypot(...offsetOf([res.ra_c, res.dec_c], req.pos)) * 3600 < 6;
        const job = same
          ? j.p
          : renderCutout(
              j.survey,
              res.ra_c,
              res.dec_c,
              (res.field_deg * j.pw) / j.ph,
              j.pw,
              j.ph,
              WISETONE
            );
        return await Promise.race([job, new Promise((r) => setTimeout(() => r(null), 300))]);
      } catch (e) {
        return null;
      }
    })();

    const { W, H, S, k, A, dx, dy } = geom();
    preparePlayer(res);
    setTimeout(() => {
      shown = true;
    }, 100);

    const tweenPromise = tween(800, (t) => {
      const p = smooth(t),
        w = S * (1 + (k - 1) * p),
        h = w * A,
        cx = W / 2 + dx * p,
        cy = H / 2 + dy * p;
      innerEl.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
      clipEl.style.clipPath = `inset(${cy - h / 2}px ${W - cx - w / 2}px ${H - cy - h / 2}px ${cx - w / 2}px round ${26 * smooth(clamp01(p * 1.3))}px)`;
    });

    wiseCanvas = await wisePromise;
    await tweenPromise;
    paintWise(res);
    spxOn = true;
    clipEl.style.visibility = "hidden";
    view = "view";
    startPlayer();
  }

  function abortHome() {
    busy = false;
    req = null;
    leave(true);
  }

  async function leave(fromError = false) {
    if (busy && !fromError && view !== "view" && view !== "waiting") return;
    stopPlayer();
    const was = view;
    view = "leaving";
    shown = false;
    selOn = false;
    if (was === "view" || was === "settling") await unsquish();
    clipEl.style.clipPath = "inset(0 0 0 0 round 0px)";
    innerEl.style.transform = "none";
    clipEl.style.visibility = "visible";
    if (overlay) {
      const o = overlay;
      overlay = null;
      o.style.transition = "opacity .5s";
      o.style.opacity = "0";
      setTimeout(() => o.remove(), 600);
    }
    view = "flying";
    const c = sky.center();
    await flyHome(c[0], 0, sky.homeFov(), 3200);
    view = "home";
    fitBlend();
    busy = false;
    req = null;
    sky.grid(false);
    zoomed = false;
    sky.startSpin(2.2);
    spinning = true;
  }

  async function unsquish() {
    const { W, H, S, k, A, dx, dy } = geom();
    clipEl.style.visibility = "visible";
    spxOn = false;
    await new Promise((r) => setTimeout(r, 300));
    await tween(900, (t) => {
      const p = 1 - smooth(t),
        w = S * (1 + (k - 1) * p),
        h = w * A,
        cx = W / 2 + dx * p,
        cy = H / 2 + dy * p;
      innerEl.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
      const g = smooth(t);
      clipEl.style.clipPath = `inset(${(cy - h / 2) * (1 - g)}px ${(W - cx - w / 2) * (1 - g)}px ${(H - cy - h / 2) * (1 - g)}px ${(cx - w / 2) * (1 - g)}px round ${26 * (1 - t)}px)`;
    });
    clipEl.style.clipPath = "inset(0 0 0 0 round 0px)";
    innerEl.style.transform = "none";
    clipEl.style.visibility = "visible";
  }

  function paintWise(res) {
    const cv = wiseCvEl;
    if (!cv || !pWiseEl) return;
    const f = pWiseEl.getBoundingClientRect(),
      d = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(f.width * d);
    cv.height = Math.round(f.height * d);
    const g = cv.getContext("2d");
    g.imageSmoothingQuality = "high";
    g.fillStyle = "#13161a";
    g.fillRect(0, 0, cv.width, cv.height);
    if (wiseCanvas) {
      const extra =
        Math.sqrt(Math.max(0, SPX_PSF ** 2 - (WISE_PSF[res.compare.band] || 6.1) ** 2)) / 2.355;
      const pxPerArcsec = cv.height / (res.field_deg * 3600);
      g.filter = `blur(${(extra * pxPerArcsec).toFixed(2)}px)`;
      g.drawImage(wiseCanvas, 0, 0, cv.width, cv.height);
      g.filter = "none";
      const im = g.getImageData(0, 0, cv.width, cv.height),
        data = im.data,
        v = [];
      for (let i = 0; i < data.length; i += 64) v.push(data[i]);
      v.sort((a, b) => a - b);
      let lo = v[Math.floor(v.length * 0.5)],
        hi = Math.max(lo + 20, v[Math.floor(v.length * 0.995)]);
      if (lo > 230) {
        lo = v[0];
        hi = 255;
      }
      for (let i = 0; i < data.length; i += 4) {
        const t = Math.min(1, Math.max(0, (data[i] - lo) / (hi - lo))),
          q = Math.asinh(t * 8) / Math.asinh(8);
        data[i] = 19 + q * 219;
        data[i + 1] = 22 + q * 218;
        data[i + 2] = 26 + q * 218;
      }
      g.putImageData(im, 0, 0);
    }
    for (const m of res.movers || []) {
      const p = toPx(res, m.ra0, m.dec0, cv.width, cv.height);
      g.save();
      g.strokeStyle = "rgba(200,204,210,.9)";
      g.lineWidth = Math.max(1.5, cv.height / 300);
      g.beginPath();
      g.arc(p[0], p[1], cv.height * 0.035, 0, Math.PI * 2);
      g.stroke();
      g.restore();
    }
  }

  function drawHl() {
    for (const [cv, pid] of [
      [wiseHlEl, pWiseEl],
      [spxHlEl, pSpxEl],
    ]) {
      if (!cv || !pid) continue;
      const f = pid.getBoundingClientRect(),
        d = Math.min(2, window.devicePixelRatio || 1);
      if (cv.width !== Math.round(f.width * d)) {
        cv.width = Math.round(f.width * d);
        cv.height = Math.round(f.height * d);
      }
      const g = cv.getContext("2d");
      g.clearRect(0, 0, cv.width, cv.height);
      const res = P || last_res;
      if (!hl || !res) continue;
      const yr = cv === wiseHlEl ? (res.compare && res.compare.epoch) || 2010.5 : null;
      const q =
        yr && (hl.pmra || hl.pmdec)
          ? propagate({ ra: hl.ra, dec: hl.dec, pmra: hl.pmra, pmdec: hl.pmdec, epoch: hl.epoch }, yr)
          : [hl.ra, hl.dec];
      const p = toPx(res, q[0], q[1], cv.width, cv.height);
      const r = Math.max(
        cv.height * 0.04,
        (cv.height * (hl.size || 0)) / 2 / (res.field_deg * 3600)
      );
      g.save();
      g.strokeStyle = "rgba(255,255,255,.95)";
      g.lineWidth = Math.max(2, cv.height / 220);
      g.shadowColor = "rgba(0,0,0,.8)";
      g.shadowBlur = 6;
      g.beginPath();
      g.arc(p[0], p[1], r, 0, Math.PI * 2);
      g.stroke();
      g.restore();
    }
  }

  function loadFrames(res) {
    return Promise.all(
      res.frames.map(
        (b) =>
          new Promise((r) => {
            const im = new Image();
            im.onload = () => r(im);
            im.onerror = () => r(im);
            im.src = "data:image/jpeg;base64," + b;
          })
      )
    ).then((a) => {
      imgs = a;
    });
  }

  function preparePlayer(res) {
    P = res;
    tpos = 0;
    playing = true;
    const cv = spxCvEl,
      f = pSpxEl.getBoundingClientRect(),
      d = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(f.width * d);
    cv.height = Math.round(f.height * d);
    drawSpx();
  }

  function startPlayer() {
    lastFrameTime = performance.now();
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(loop);
  }
  function stopPlayer() {
    cancelAnimationFrame(raf);
    P = null;
  }

  function loop(t) {
    if (!P) return;
    const dt = Math.max(0, Math.min(0.1, (t - lastFrameTime) / 1000));
    lastFrameTime = t;
    if (playing && !dragging) tpos = (tpos + dt * FPS + imgs.length) % imgs.length;
    drawSpx();
    raf = requestAnimationFrame(loop);
  }

  const moverAt = (m, year) =>
    propagate({ ra: m.ra0, dec: m.dec0, pmra: m.pmra, pmdec: m.pmdec, epoch: m.epoch0 }, year);

  function ghost(g, x0, y0, x1, y1, size) {
    g.save();
    g.strokeStyle = "rgba(200,204,210,.85)";
    g.lineWidth = Math.max(1.5, size / 300);
    g.beginPath();
    g.arc(x0, y0, size * 0.045, 0, Math.PI * 2);
    g.stroke();
    const d = Math.hypot(x1 - x0, y1 - y0),
      r0 = size * 0.045,
      r1 = size * 0.055;
    if (d > r0 + r1) {
      const ux = (x1 - x0) / d,
        uy = (y1 - y0) / d;
      g.setLineDash([size / 90, size / 110]);
      g.beginPath();
      g.moveTo(x0 + ux * r0, y0 + uy * r0);
      g.lineTo(x1 - ux * r1, y1 - uy * r1);
      g.stroke();
    }
    g.restore();
  }

  function ring(g, x, y, size, a = 0.85) {
    if (x < -size * 0.1 || x > size * 1.1) return;
    g.save();
    g.strokeStyle = `rgba(190,120,132,${a})`;
    g.lineWidth = Math.max(2, size / 240);
    g.beginPath();
    g.arc(x, y, size * 0.055, 0, Math.PI * 2);
    g.stroke();
    g.restore();
  }

  function drawSpx() {
    const cv = spxCvEl;
    if (!cv) return;
    const g = cv.getContext("2d"),
      n = imgs.length;
    if (!n || !P) return;
    const i = Math.floor(tpos) % n,
      fr = tpos - Math.floor(tpos),
      j = (i + 1) % n,
      e = smooth(fr);
    const cw = cv.width,
      ch = cv.height,
      S = ch,
      ox = (cw - S) / 2;
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = "high";
    g.filter = `blur(${((S / P.nx) * 0.25).toFixed(1)}px)`;
    g.globalAlpha = 1;
    if (imgs[i] && imgs[i].complete && imgs[i].naturalWidth) g.drawImage(imgs[i], ox, 0, S, S);
    if (e > 0.003 && imgs[j] && imgs[j].complete && imgs[j].naturalWidth) {
      g.globalAlpha = e;
      g.drawImage(imgs[j], ox, 0, S, S);
      g.globalAlpha = 1;
    }
    g.filter = "none";
    const yr = P.years ? (j > i ? P.years[i] + (P.years[j] - P.years[i]) * e : P.years[i]) : midEpoch;
    for (const m of P.movers || []) {
      const p0 = toPx(P, m.ra0, m.dec0, cw, ch),
        q = moverAt(m, yr),
        p1 = toPx(P, q[0], q[1], cw, ch);
      ghost(g, p0[0], p0[1], p1[0], p1[1], S * 0.78);
      ring(g, p1[0], p1[1], S * 0.64, 0.95);
    }
    const k = Math.round(tpos) % n;
    spxCap = P.dates[k] || "";
    const frac = n > 1 ? tpos / (n - 1) : 0,
      tr = trackEl ? trackEl.clientWidth : 0;
    fillWidth = Math.min(1, frac) * tr;
    thumbLeft = Math.min(1, frac) * tr;
  }

  function seek(e) {
    if (!trackEl) return;
    const r = trackEl.getBoundingClientRect();
    tpos = clamp01((e.clientX - r.left) / r.width) * Math.max(1, imgs.length - 1);
    if (P) drawSpx();
  }

  function presetItems() {
    return presets.map((p) => Object.assign({}, p, { kind: KINDS[p.key] || "" }));
  }

  function loadCatalog() {
    if (!catLoading) {
      catLoading = fetch("/catalog.json")
        .then((r) => r.json())
        .then((rows) => {
          CATALOG = rows.map(([name, ra, dec, pmra, pmdec, otype, size, nb]) => ({
            key: "custom",
            name,
            ra,
            dec,
            pmra,
            pmdec,
            epoch: 2000,
            kind: OTYPE[otype] || otype,
            size,
            nb,
            lc: name.toLowerCase(),
          }));
          if (searchQuery.trim()) onSearchInput();
          return CATALOG;
        })
        .catch(() => (CATALOG = []));
    }
    return catLoading;
  }

  function famous(q) {
    const s = q.toLowerCase().replace(/\s+/g, " ").trim(),
      sq = s.replace(/\s/g, "");
    const out = FAMOUS.filter((f) => f.name.toLowerCase().includes(s));
    if (CATALOG) {
      const hit = [];
      for (const c of CATALOG) {
        if (c.lc.startsWith(s) || c.lc.replace(/\s/g, "").startsWith(sq) || c.lc.includes(" " + s)) {
          hit.push(c);
          if (hit.length > 40) break;
        }
      }
      hit.sort(
        (a, b) =>
          (b.lc === s || b.lc.replace(/\s/g, "") === sq) -
            (a.lc === s || a.lc.replace(/\s/g, "") === sq) ||
          b.lc.startsWith(s) - a.lc.startsWith(s) ||
          b.nb - a.nb
      );
      const seenPos = new Set(out.map((o) => o.ra.toFixed(2) + o.dec.toFixed(2)));
      for (const h of hit) {
        const k = h.ra.toFixed(2) + h.dec.toFixed(2);
        if (!seenPos.has(k)) {
          seenPos.add(k);
          out.push(h);
        }
      }
    }
    return out.slice(0, 8);
  }

  const timed = (url, ms) => {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), ms);
    return fetch(url, { signal: c.signal }).finally(() => clearTimeout(t));
  };

  async function remote(q, my, have) {
    const esc = (s) => s.replace(/'/g, "''");
    const cap = q[0].toUpperCase() + q.slice(1),
      vars = [...new Set([q, cap, q.toUpperCase()])];
    const like = vars
      .flatMap((v) => [`'${esc(v)}%'`, `'NAME ${esc(v)}%'`, `'* ${esc(v)}%'`, `'V* ${esc(v)}%'`])
      .map((p) => `id LIKE ${p}`)
      .join(" OR ");
    const adql = `SELECT TOP 8 main_id, ra, dec, pmra, pmdec, otype, galdim_majaxis FROM basic JOIN ident ON oid = oidref WHERE ${like}`;
    const simbad = timed(
      "https://simbad.harvard.edu/simbad/sim-tap/sync?request=doQuery&lang=adql&format=json&query=" +
        encodeURIComponent(adql),
      7000
    )
      .then((r) => r.json())
      .then((j) =>
        (j.data || [])
          .filter((x) => x[1] != null)
          .map((x) => ({
            key: "custom",
            name: String(x[0]).replace(/^NAME\s+/, "").replace(/^\*\s+/, ""),
            ra: x[1],
            dec: x[2],
            pmra: x[3] || 0,
            pmdec: x[4] || 0,
            epoch: 2000,
            kind: OTYPE[x[5]] || x[5] || "",
            size: x[6] || 0,
          }))
      )
      .catch(() => []);
    const mirrors = [
      "https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/SNV?",
      "https://vizier.cfa.harvard.edu/viz-bin/nph-sesame/-oI/SNV?",
      "https://vizier.idia.ac.za/viz-bin/nph-sesame/-oI/SNV?",
    ];
    const sesame = Promise.any(
      mirrors.map((m) =>
        timed(m + encodeURIComponent(q), 6000)
          .then((r) => r.text())
          .then((t) => {
            const o = parseSesame(t, q);
            if (!o.length) throw 0;
            return o;
          })
      )
    ).catch(() => []);
    const tidy = (n) => n.replace(/^(NAME|V\*|\*\*|\*)\s+/, "").replace(/\s+/g, " ").trim();
    const spot = (o) => (+o.ra).toFixed(2) + "," + (+o.dec).toFixed(2);
    const show = (out) => {
      if (my !== qid || !searching || !out.length) return;
      out.forEach((o) => {
        o.name = tidy(o.name);
      });
      const seen = new Set(have.map((h) => h.name.toLowerCase())),
        seenAt = new Set(have.map(spot));
      const add = out.filter((o) => {
        const k = spot(o);
        if (seen.has(o.name.toLowerCase()) || seenAt.has(k)) return false;
        seenAt.add(k);
        return true;
      });
      have = have.concat(add).slice(0, 8);
      searchItems = have;
    };
    sesame.then(show);
    simbad.then(show);
  }

  function onSearchInput() {
    clearTimeout(deb);
    const q = searchQuery.trim(),
      my = ++qid;
    if (!q) {
      searchItems = presetItems();
      return;
    }
    const local = presetItems()
      .filter((p) => p.name.toLowerCase().includes(q.toLowerCase()))
      .concat(famous(q));
    const num = parseCoords(q, midEpoch);
    const first = num ? [num] : local;
    searchItems = first;
    if (!num && q.length >= 2) deb = setTimeout(() => remote(q, my, first), 260);
  }

  function openSearch() {
    if (view !== "home" && view !== "view") return;
    loadCatalog();
    closeAbout();
    searching = true;
    tipOn = false;
    searchItems = presetItems();
    searchSel = -1;
    setTimeout(() => qEl && qEl.focus(), 120);
  }

  function closeSearch(quick = false) {
    searching = false;
    if (qEl) qEl.blur();
    if (!quick) {
      setTimeout(() => {
        if (!searching) searchQuery = "";
      }, 400);
    }
  }

  function choose(it) {
    const fov =
      it.key !== "custom" ? it.fov : Math.min(5400, Math.max(300, 1.3 * 60 * (it.size || 0)));
    go(
      Object.assign({ epoch: 2000, pmra: 0, pmdec: 0, detector: "SPHEREx-D3" }, it, {
        fov: fov || 300,
      })
    );
  }

  function openAbout(which = "how") {
    if (busy && view !== "view") return;
    closeSearch(true);
    aboutPage = which;
  }

  function closeAbout() {
    aboutPage = null;
  }

  // Action directive for search thumbnails
  function thumbAction(node, it) {
    const key = it.key + "|" + it.name;
    if (thumbCache.has(key)) {
      const clone = thumbCache.get(key).cloneNode(true);
      clone.getContext("2d").drawImage(thumbCache.get(key), 0, 0);
      node.appendChild(clone);
      clone.classList.add("ok");
      return;
    }
    thumbQ.push({ it, box: node, key });
    pumpThumbs();
  }

  async function pumpThumbs() {
    if (thumbBusy) return;
    thumbBusy = true;
    while (thumbQ.length) {
      const { it, box, key } = thumbQ.shift();
      if (!box.isConnected) continue;
      try {
        const p = propagate(it, 1995.5);
        const c = await renderCutout(SURVEYS.dss, p[0], p[1], 0.05, 96, 96, {
          gain: 1.5,
          gamma: 0.85,
          black: 0.04,
        });
        thumbCache.set(key, c);
        if (box.isConnected) {
          const k = c.cloneNode();
          k.getContext("2d").drawImage(c, 0, 0);
          box.appendChild(k);
          requestAnimationFrame(() => k.classList.add("ok"));
        }
      } catch (e) {}
    }
    thumbBusy = false;
  }

  function objThumbAction(node, o) {
    if (!stageResult) return;
    const fov = Math.min(stageResult.field_deg, Math.max(0.025, (1.5 * (o.size || 0)) / 3600));
    objThumbQ.push({ o, box: node, fov });
    pumpObj();
  }

  async function pumpObj() {
    if (objBusy) return;
    objBusy = true;
    while (objThumbQ.length) {
      const { o, box, fov } = objThumbQ.shift();
      if (!box.isConnected) continue;
      try {
        const c = await renderCutout(SURVEYS.tmass, o.ra, o.dec, fov, 88, 88, {
          gain: 1.3,
          gamma: 0.85,
          black: 0.03,
        });
        if (box.isConnected) {
          box.appendChild(c);
          requestAnimationFrame(() => c.classList.add("ok"));
        }
      } catch (e) {}
    }
    objBusy = false;
  }

  function bindSky() {
    const rect = () => clipEl.getBoundingClientRect();
    clipEl.addEventListener(
      "pointerdown",
      () => {
        smoothId++;
        if (smoothCv) smoothCv.classList.remove("on");
      },
      true
    );
    window.addEventListener("pointerup", () => {
      if (view === "home" && sky.fov() < 8) setTimeout(refreshSmooth, 200);
    });
    const homeOk = () => view === "home" && !busy && !searching && !aboutPage;
    let tipUntil = 0,
      tipDone = false;
    window.addEventListener("pointermove", (e) => {
      if (!homeOk() || tipDone || !clipEl.contains(e.target)) {
        if (tipUntil && performance.now() > tipUntil) {
          tipOn = false;
          tipDone = true;
        }
        return;
      }
      if (!tipUntil) tipUntil = performance.now() + 4500;
      if (performance.now() > tipUntil) {
        tipOn = false;
        tipDone = true;
        return;
      }
      tipTransform = `translate(${e.clientX + 18}px,${e.clientY + 20}px)`;
      tipOn = true;
    });

    let wt = 0;
    clipEl.addEventListener(
      "wheel",
      (e) => {
        if (!homeOk()) return;
        e.preventDefault();
        e.stopPropagation();
        wake();
        const dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY;
        const f = Math.min(sky.homeFov(), Math.max(0.02, sky.fov() * Math.exp(dy * 0.004)));
        const r = rect(),
          before = sky.pix2world(e.clientX - r.left, e.clientY - r.top);
        sky.a.setFoV(f);
        fitBlend();
        const after = sky.pix2world(e.clientX - r.left, e.clientY - r.top),
          c = sky.center();
        if (before && after && f < 60) {
          sky.a.gotoRaDec(
            c[0] + (before[0] - after[0]),
            Math.max(-90, Math.min(90, c[1] + before[1] - after[1]))
          );
        }
        clearTimeout(wt);
        if (smoothCv) smoothCv.classList.remove("on");
        smoothId++;
        wt = setTimeout(refreshSmooth, 300);
      },
      { capture: true, passive: false }
    );

    clipEl.addEventListener(
      "contextmenu",
      (e) => {
        e.preventDefault();
        e.stopPropagation();
      },
      true
    );
    window.addEventListener(
      "contextmenu",
      (e) => {
        if (clipEl.contains(e.target)) {
          e.preventDefault();
          e.stopPropagation();
        }
      },
      true
    );

    let sel = null;
    const stop = (e) => {
      e.stopPropagation();
      e.preventDefault();
    };
    const drawSel = () => {
      const l = Math.min(sel.x0, sel.x1),
        t = Math.min(sel.y0, sel.y1);
      selStyle = `left:${l}px;top:${t}px;width:${Math.abs(sel.x1 - sel.x0)}px;height:${Math.abs(sel.y1 - sel.y0)}px`;
    };
    const isRightClick = (e) => e.button === 2 || (e.button === 0 && e.ctrlKey);
    clipEl.addEventListener(
      "pointerdown",
      (e) => {
        if (!isRightClick(e)) {
          if (homeOk() && e.button === 0) wake();
          return;
        }
        stop(e);
        if (!homeOk() || sel) return;
        wake();
        const r = rect();
        sel = {
          x0: e.clientX - r.left,
          y0: e.clientY - r.top,
          x1: e.clientX - r.left,
          y1: e.clientY - r.top,
        };
        drawSel();
        selOn = true;
        tipDone = true;
        tipOn = false;
      },
      true
    );
    clipEl.addEventListener(
      "mousedown",
      (e) => {
        if (isRightClick(e)) stop(e);
      },
      true
    );
    window.addEventListener(
      "pointermove",
      (e) => {
        if (!sel) return;
        stop(e);
        const r = rect(),
          A = panelA(),
          dx = e.clientX - r.left - sel.x0,
          dy = e.clientY - r.top - sel.y0;
        const w = Math.max(Math.abs(dx), Math.abs(dy) / A);
        sel.x1 = sel.x0 + Math.sign(dx || 1) * w;
        sel.y1 = sel.y0 + Math.sign(dy || 1) * w * A;
        drawSel();
      },
      true
    );
    window.addEventListener(
      "mousemove",
      (e) => {
        if (sel) stop(e);
      },
      true
    );
    window.addEventListener(
      "mouseup",
      (e) => {
        if (sel && isRightClick(e)) stop(e);
      },
      true
    );
    window.addEventListener(
      "pointercancel",
      () => {
        if (sel) {
          sel = null;
          selOn = false;
        }
      },
      true
    );
    window.addEventListener(
      "pointerup",
      async (e) => {
        if (!sel || (e.button !== 2 && e.button !== 0)) return;
        stop(e);
        const s0 = sel;
        sel = null;
        selOn = false;
        const l = Math.min(s0.x0, s0.x1),
          r_ = Math.max(s0.x0, s0.x1),
          t = Math.min(s0.y0, s0.y1),
          b = Math.max(s0.y0, s0.y1);
        const cx = (l + r_) / 2,
          cy = (t + b) / 2;
        const isClick = r_ - l < 4 || b - t < 4;
        const c = sky.pix2world(cx, cy);
        if (!c) {
          return toast("Keep the area inside the map.");
        }
        let side = 180;
        if (isClick) {
          side = Math.min(900, Math.max(120, Math.round(sky.fov() * 3600 * 0.05)));
        } else {
          const L = sky.pix2world(l, cy),
            R = sky.pix2world(r_, cy),
            T = sky.pix2world(cx, t),
            B = sky.pix2world(cx, b);
          if (!L || !R || !T || !B) {
            return toast("Keep the area inside the map.");
          }
          side = sepDeg(T, B) * 3600;
          if (side > 5400) {
            return toast(
              `That area is ${(side / 3600).toFixed(side > 36000 ? 0 : 1)}° wide. Scroll to zoom in and pick one under 1.5°.`
            );
          }
        }

        let targetRa = c[0],
          targetDec = c[1],
          targetName = "Selected area";
        try {
          const sw = 48,
            sh = 48;
          const cutout = await Promise.race([
            renderCutout(SURVEYS.tmass, c[0], c[1], side / 3600, sw, sh, {}),
            new Promise((r) => setTimeout(() => r(null), 400)),
          ]);
          if (cutout) {
            const data = cutout.getContext("2d").getImageData(0, 0, sw, sh).data;
            let maxB = 0,
              bestX = sw / 2,
              bestY = sh / 2;
            for (let py = 0; py < sh; py++) {
              for (let px = 0; px < sw; px++) {
                const idx = (py * sw + px) * 4;
                const bright = data[idx] + data[idx + 1] + data[idx + 2];
                if (bright > maxB) {
                  maxB = bright;
                  bestX = px;
                  bestY = py;
                }
              }
            }
            if (maxB > 60) {
              const fracX = (bestX + 0.5 - sw / 2) / sw,
                fracY = (sh / 2 - (bestY + 0.5)) / sh;
              const degW = side / 3600;
              const dRa = (-fracX * degW) / Math.cos((c[1] * Math.PI) / 180),
                dDec = fracY * degW;
              targetRa = ((c[0] + dRa) % 360 + 360) % 360;
              targetDec = Math.max(-90, Math.min(90, c[1] + dDec));
            }
          }
        } catch (err) {}
        go({
          key: "custom",
          name: targetName,
          ra: targetRa,
          dec: targetDec,
          pmra: 0,
          pmdec: 0,
          epoch: midEpoch,
          fov: Math.max(60, side),
        });
      },
      true
    );
  }

  onMount(async () => {
    loadCatalog();
    // Fetch presets from backend
    fetch("/api/presets")
      .then((r) => r.json())
      .then((d) => {
        presets = d.presets || [];
        midEpoch = d.mid || 2026.3;
      })
      .catch(() => {});

    smoothCv = document.createElement("canvas");
    smoothCv.id = "smooth";
    innerEl.appendChild(smoothCv);

    sky = new Sky(skyEl);
    await sky.init();
    sky.set(0, 0, sky.homeFov());
    fitBlend();
    sky.startSpin(2.2);
    spinning = true;
    bindSky();

    window.addEventListener("resize", () => {
      fitBlend();
      if (view === "home") sky.a.setFoV(sky.homeFov());
    });

    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (searching) closeSearch();
        else if (aboutPage) closeAbout();
      }
      if (e.key === " " && view === "view" && document.activeElement === document.body) {
        e.preventDefault();
        playing = !playing;
      }
    });

    // URL query param deep linking (?ra=...&dec=...&fov=...)
    const qp = new URLSearchParams(window.location.search);
    if (qp.has("ra") && qp.has("dec")) {
      const qra = parseFloat(qp.get("ra")),
        qdec = parseFloat(qp.get("dec")),
        qfov = parseFloat(qp.get("fov") || "180");
      if (isFinite(qra) && isFinite(qdec)) {
        go({
          key: "custom",
          name: "Linked location",
          ra: qra,
          dec: qdec,
          pmra: 0,
          pmdec: 0,
          epoch: midEpoch,
          fov: isFinite(qfov) ? qfov : 180,
        });
      }
    }
  });

  onDestroy(() => {
    stopPlayer();
    if (progInterval) clearInterval(progInterval);
  });

  $effect(() => {
    hl;
    drawHl();
  });
</script>

<div
  id="app"
  data-view={view}
  class:shown
  class:searching
  class:about={aboutPage !== null}
  class:dimming
  class:smoothready
  class:zoomed
>
  <!-- the sky: one Aladin map, clipped and scaled by CSS when it squishes into the left panel -->
  <div id="sky-clip" bind:this={clipEl}>
    <div id="sky-inner" bind:this={innerEl}>
      <div id="sky" bind:this={skyEl}></div>
    </div>
  </div>
  <div id="blend" bind:this={blendEl}></div>
  <div id="sel" class:on={selOn} style={selStyle} aria-hidden="true"></div>
  <div id="tip" class:on={tipOn} style="transform:{tipTransform}" aria-hidden="true">
    Right-click and drag to select an area
  </div>
  <div id="beacon" aria-hidden="true"></div>
  <div id="side-prog" role="status" aria-live="polite">
    <div id="side-prog-head">
      <span id="side-prog-pct">{Math.round(progWidth)}%</span>
      <span id="side-prog-label">Fetching from TAP API · EC2</span>
    </div>
    <div id="prog-wrap" aria-hidden="true">
      <div id="prog-bar" bind:this={progBarEl} style="width:{progWidth}%"></div>
    </div>
  </div>
  <div id="veil" onclick={() => closeSearch()}></div>

  <header id="bar">
    <button
      id="brand"
      type="button"
      onclick={() => {
        closeSearch(true);
        if (aboutPage) closeAbout();
        if (view === "view" || view === "waiting") leave();
      }}
    >
      Draconid Drift
    </button>
    <nav>
      {#each [["targets", "Targets"], ["how", "How it works"], ["data", "Data"]] as [pg, label]}
        <button
          class="navb"
          class:on={aboutPage === pg}
          data-page={pg}
          type="button"
          onclick={() => (aboutPage === pg ? closeAbout() : openAbout(pg))}
        >
          {label}
        </button>
      {/each}
      <div id="search" class:open={searching}>
        <div id="sfield">
          <input
            id="q"
            bind:this={qEl}
            bind:value={searchQuery}
            oninput={onSearchInput}
            onkeydown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                searchSel = Math.min(searchItems.length - 1, searchSel + 1);
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                searchSel = Math.max(0, searchSel - 1);
              } else if (e.key === "Enter" && searchItems.length) {
                choose(searchItems[Math.max(0, searchSel)]);
              }
            }}
            type="text"
            placeholder="Search for an object"
            autocomplete="off"
            spellcheck="false"
            aria-label="Search for an object"
          />
        </div>
        <button
          id="sbtn"
          type="button"
          aria-label="Search"
          aria-expanded={searching}
          onclick={() => (searching ? closeSearch() : openSearch())}
        >
          <svg
            viewBox="0 0 24 24"
            width="22"
            height="22"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
          >
            <circle class="lens" cx="10.5" cy="10.5" r="6.2" />
            <path class="handle" d="M15.2 15.2 20 20" />
            <path class="x1" d="M6 6 18 18" />
            <path class="x2" d="M18 6 6 18" />
          </svg>
        </button>
        <ul id="results" role="listbox">
          {#each searchItems as it, i (it.key + "|" + it.name + "|" + i)}
            <li
              role="option"
              aria-selected={searchSel === i}
              class:on={searchSel === i}
              onclick={() => choose(it)}
              onmouseenter={() => (searchSel = i)}
            >
              <div class="th" use:thumbAction={it}></div>
              <div>
                <div class="nm">{it.name}</div>
                <div class="kd">{it.kind || ""}</div>
              </div>
            </li>
          {/each}
        </ul>
      </div>
    </nav>
  </header>

  <main id="page2">
    <section class="panel img" id="pWise" bind:this={pWiseEl}>
      <canvas id="wiseCv" bind:this={wiseCvEl}></canvas>
      <canvas class="hl" id="wiseHl" bind:this={wiseHlEl}></canvas>
      <div class="lab">
        <h2 id="wiseH">{stageResult?.compare?.name || "AllWISE"}</h2>
        <p id="wiseCap">{stageResult?.compare?.year || "2010"}</p>
      </div>
    </section>

    <section class="panel img" id="pSpx" bind:this={pSpxEl}>
      <canvas id="spxCv" class:on={spxOn} bind:this={spxCvEl}></canvas>
      <canvas class="hl" id="spxHl" bind:this={spxHlEl}></canvas>
      <div class="lab">
        <h2>SPHEREx</h2>
        <p id="spxCap">{spxCap}</p>
      </div>
      <div class="ctrl">
        <button
          id="play"
          class:paused={!playing}
          type="button"
          aria-label="Pause"
          onclick={() => (playing = !playing)}
        >
          <svg viewBox="0 0 24 24" width="16" height="16">
            <g class="ip">
              <rect x="6" y="5" width="4" height="14" rx="1" />
              <rect x="14" y="5" width="4" height="14" rx="1" />
            </g>
            <path class="ia" d="M7 5v14l12-7z" />
          </svg>
        </button>
        <div
          id="track"
          bind:this={trackEl}
          onpointerdown={(e) => {
            dragging = true;
            trackEl.setPointerCapture(e.pointerId);
            seek(e);
          }}
          onpointermove={(e) => {
            if (dragging) seek(e);
          }}
          onpointerup={() => (dragging = false)}
        >
          <div id="fill" style="width:{fillWidth}px"></div>
          <div id="thumb" style="left:{thumbLeft}px"></div>
        </div>
      </div>
    </section>

    <aside class="panel" id="info">
      <div class="blk">
        <h3>What we are looking at</h3>
        <div class="big" id="iName">{stageResult?.name || ""}</div>
      </div>
      <div class="blk">
        <h3>Coords</h3>
        <div class="coord" id="iCoord">
          {#if stageResult}
            {@const [a, b] = sexa(stageResult.ra, stageResult.dec)}
            {a}<br />{b}
          {/if}
        </div>
      </div>
      <div class="blk">
        <h3>More info</h3>
        <div id="iVerdict">
          {#if stageResult?.verdict}
            {@const v = stageResult.verdict}
            <div class="chip tone-{v.tone}"><i></i>{v.word}</div>
            <div class="conf">Confidence <b>{v.conf}</b></div>
            <div class="dots">
              {#each v.checks as c}
                <span class={c.ok ? "ok" : "no"}><em></em>{c.k}</span>
              {/each}
            </div>
          {/if}
        </div>
      </div>
      <div class="blk grow">
        <h3>In the frame</h3>
        <ul class="objs" id="iObjs">
          {#if stageResult}
            {#if !(stageResult.objects && stageResult.objects.length)}
              <li class="none">Nothing bright enough to list</li>
            {:else}
              {#each stageResult.objects as o}
                <li
                  class:on={hl === o}
                  onmouseenter={() => (hl = o)}
                  onmouseleave={() => {
                    if (hl === o) hl = null;
                  }}
                >
                  <div class="th" use:objThumbAction={o}></div>
                  <div>
                    <div class="nm">{o.name || o.kind}</div>
                    <div class="kd">{o.name ? o.kind : "Unnamed"}</div>
                  </div>
                </li>
              {/each}
            {/if}
          {/if}
        </ul>
      </div>
    </aside>
  </main>
  <footer id="foot"></footer>

  <section id="about" aria-hidden={aboutPage === null}>
    <div class="pg" class:on={aboutPage === "targets"} data-page="targets">
      <h1>Targets</h1>
      <p>
        Stars and brown dwarfs close enough to the Sun that SPHEREx sees them move. Pick one to fly
        there.
      </p>
      <ul id="tlist">
        {#each presetItems() as p}
          <li>
            <button
              type="button"
              onclick={() => {
                closeAbout();
                choose(p);
              }}
            >
              <span>{p.name}</span>
              <small>{p.kind || ""}</small>
            </button>
          </li>
        {/each}
      </ul>
    </div>
    <div class="pg" class:on={aboutPage === "how"} data-page="how">
      <h1>How it works</h1>
      <p>
        SPHEREx maps the whole sky every six months. Draconid Drift pulls every SPHEREx exposure that
        covers the spot you choose, cleans and lines them up, and plays them in time order.
      </p>
      <p>
        To see motion over decades, the left panel shows the same patch from an older survey: 2MASS
        (about 2000) or AllWISE (2010), blurred to SPHEREx's sharpness so the two compare fairly.
      </p>
      <p>
        A star counts as moving when it is clearly detected where it should be now and its old
        position is empty. Each claim is checked against random spots in the same frames, so the
        confidence shown is how often noise alone would pass.
      </p>
      <p>
        The list beside the images names what is in view from SIMBAD. Bright sources with no name are
        sorted into star, galaxy or nebula by a small model trained on SPHEREx sources that SIMBAD has
        already identified.
      </p>
    </div>
    <div class="pg" class:on={aboutPage === "data"} data-page="data">
      <h1>Data</h1>
      <p><b>SPHEREx</b> spectral images, NASA/IPAC Infrared Science Archive.</p>
      <p><b>2MASS</b> and <b>AllWISE</b> sky maps, served as HiPS by IRSA and CDS.</p>
      <p><b>Gaia DR3</b> proper motions, ESA.</p>
      <p><b>SIMBAD</b> object names and types, CDS Strasbourg (Harvard mirror).</p>
      <p>Built for the NASA Space Apps Challenge 2026, Planet X and SPHEREx.</p>
    </div>
  </section>
  <div id="toast" class:on={toastOn} role="status">{toastMsg}</div>
</div>
