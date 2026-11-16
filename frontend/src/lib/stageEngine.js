import { Sky, unit, radec, slerp } from "./sky.js";
import { renderCutout, SURVEYS } from "./healpix.js";
import * as intro from "./intro.js";
import * as depth from "./depth.js";

export function mountStage() {
/* ---------------------------------------------------------------- FastAPI Stage API bridge */
let args = { presets: [], mid: 2026.3, result: null, error: null };
async function emit(value) {
  if (value.type === "home") {
    args.result = null;
    args.error = null;
    return;
  }
  if (value.type === "go") {
    try {
      const resp = await fetch("/api/stage/go", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value),
      });
      const data = await resp.json();
      if (!req || req.nonce !== value.nonce) return;
      if (data.error || !resp.ok) {
        args.error = data.error || `Server error (${resp.status})`;
        args.result = null;
      } else {
        args.error = null;
        args.result = data;
      }
      onArgs();
    } catch (err) {
      if (!req || req.nonce !== value.nonce) return;
      args.error = "Unable to reach the Draconid Drift server.";
      args.result = null;
      onArgs();
    }
    return;
  }
  if (value.type === "bands") {
    const r = last_res || args.result;
    if (!r) return;
    try {
      const resp = await fetch("/api/stage/bands", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: value.key || null,
          name: r.name,
          ra: r.ra,
          dec: r.dec,
          pmra: r.pmra || 0,
          pmdec: r.pmdec || 0,
          epoch: r.epoch,
          ra_c: r.ra_c,
          dec_c: r.dec_c,
          field_deg: r.field_deg,
          detector: r.detector || "SPHEREx-D3",
          objects: r.objects || [],
        }),
      });
      const b = await resp.json();
      if (args.result && args.result.nonce === value.for) {
        args.result = Object.assign({}, args.result, { bands: b.bands || [], spectra: b.spectra || {} });
        onArgs();
      }
    } catch (err) {
      if (args.result && args.result.nonce === value.for) {
        args.result = Object.assign({}, args.result, { bands: [], spectra: {} });
        onArgs();
      }
    }
    return;
  }
  if (value.type === "depth") {
    const r = last_res || args.result;
    if (!r) return;
    try {
      const resp = await fetch("/api/stage/depth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ra_c: r.ra_c,
          dec_c: r.dec_c,
          field_deg: r.field_deg,
        }),
      });
      const d = await resp.json();
      if (args.result && args.result.nonce === value.for) {
        args.result = Object.assign({}, args.result, { depth: d.depth });
        onArgs();
      }
    } catch (err) {
      if (args.result && args.result.nonce === value.for) {
        args.result = Object.assign({}, args.result, {
          depth: { objects: [], error: "The distance catalogues did not answer. Try again in a moment." },
        });
        onArgs();
      }
    }
  }
}

/* ---------------------------------------------------------------- clock (real, or stepped by tests) */
let manual = false, mclock = 0; const queue = [];
const now = () => (manual ? mclock : performance.now());
const later = f => (manual ? queue.push(f) : requestAnimationFrame(f));
window.__manual = () => { manual = true; mclock = 0; };
window.__advance = ms => { mclock += ms; queue.splice(0).forEach(f => f(mclock)); };

const smooth = x => x * x * (3 - 2 * x);
const clamp01 = x => Math.max(0, Math.min(1, x));
// slow to leave, quickens toward the end, then settles
const flightEase = x => smooth(Math.pow(clamp01(x), 1.35));
// zoom in log-scale with a long soft landing, so the map tiles keep up and it stops exactly on the field
const zoomEase = x => 1 - Math.pow(1 - x, 3);
const tween = (dur, fn) => new Promise(res => {
  const t0 = now();
  const step = () => { const t = clamp01((now() - t0) / dur); fn(t); t < 1 ? later(step) : res(); };
  later(step);
});

/* ---------------------------------------------------------------- dom */
const $ = id => document.getElementById(id);
const app = $("app"), skyEl = $("sky"), clip = $("sky-clip"), inner = $("sky-inner"), selEl = $("sel"), tipEl = $("tip");
const sky = new Sky(skyEl);
let view = "home", busy = false, req = null, wiseCanvas = null, skyCanvas = null, overlay = null;
// one grey scale for everything: light sources on the page colour
const MAPTONE = { mono: true, black: 0.10, gain: 1.25, gamma: 1.0 }, WISETONE = { mono: true, black: 0, gain: 1, gamma: 1 };
const setView = v => { view = v; app.dataset.view = v; };

/* ---------------------------------------------------------------- helpers */
const MAS = 3.6e6, PX = 6.15, FRAME_FACTOR = 3.0;
const detNum = d => +(String(d || "SPHEREx-D3").slice(-1)) || 3;
// comparison image per SPHEREx detector: 2MASS K/H/J (~2000) for D1-D3, AllWISE (2010.5) for D4-D6 (same wavelength band)
const compareFor = d => detNum(d) <= 3 ? { epoch: 2000.0, survey: [null, SURVEYS.tj, SURVEYS.th, SURVEYS.tk][detNum(d)] }
                                       : { epoch: 2010.5, survey: detNum(d) === 4 ? SURVEYS.w1 : SURVEYS.w2 };
function propagate(it, year) {
  const dt = year - (it.epoch ?? 2000);
  const dec1 = it.dec + (it.pmdec || 0) * dt / MAS;
  const ra1 = it.ra + (it.pmra || 0) * dt / MAS / Math.cos(((it.dec + dec1) / 2) * Math.PI / 180);
  return [ra1, dec1];
}
const fieldDeg = fov => { let n = Math.ceil(fov / PX); if (n % 2 === 0) n += 1; return n * PX / 3600; };
function sexa(ra, dec) {
  const h = ra / 15, H = Math.floor(h), M = Math.floor((h - H) * 60), S = ((h - H) * 60 - M) * 60;
  const a = Math.abs(dec), D = Math.floor(a), m = Math.floor((a - D) * 60), s = Math.round(((a - D) * 60 - m) * 60);
  return [`RA   ${H}h ${String(M).padStart(2, "0")}m ${S.toFixed(1).padStart(4, "0")}s`, `Dec ${dec < 0 ? "−" : "+"}${String(D).padStart(2, "0")}° ${String(m).padStart(2, "0")}′ ${String(s).padStart(2, "0")}″`];
}
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.id); toast.id = setTimeout(() => t.classList.remove("on"), msg.startsWith("Something went wrong") ? 15000 : 4200); }

const KINDS = { barnard: "Red dwarf, fast mover", epsindi: "Brown dwarf pair", wise0855: "Coldest known brown dwarf", rwaur: "Young variable star", rdra: "Mira variable star" };
const OTYPE = { "*": "Star", "**": "Double star", "BY*": "Star", "Mi*": "Mira variable", "BD*": "Brown dwarf", "Y*O": "Young star", "Er*": "Flare star",
  "QSO": "Quasar", "G": "Galaxy", "AGN": "Active galaxy", "PN": "Nebula", "HII": "Nebula", "SNR": "Supernova remnant", "Psr": "Pulsar", "HXB": "X-ray binary",
  "V*": "Variable star", "Pu*": "Pulsating star", "Ro*": "Rotating star", "WD*": "White dwarf", "RGB*": "Giant star", "LM*": "Low-mass star", "Cl*": "Star cluster", "GlC": "Globular cluster" };

/* ---------------------------------------------------------------- sky + home */
// Aladin draws the zoomed map as hard pixels; when zoomed in, a smoothed copy of the same tiles covers it
const smoothCv = document.createElement("canvas"); smoothCv.id = "smooth"; inner.appendChild(smoothCv);
let smoothId = 0;
async function refreshSmooth() {
  const my = ++smoothId; smoothCv.classList.remove("on");
  if (view !== "home" || sky.fov() > 3.5) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const [r, d] = sky.center(), w = Math.min(1800, Math.round(sky.W * dpr)), h = Math.round(w * sky.H / sky.W);
  try {
    const c = await renderCutout(SURVEYS.tmass, r, d, sky.fov(), w, h, { gain: 1.08, gamma: 0.96 });
    if (my !== smoothId || view !== "home") return;
    smoothCv.width = w; smoothCv.height = h; smoothCv.getContext("2d").drawImage(c, 0, 0); smoothCv.classList.add("on");
  } catch (e) {}
}
async function autoCuts() { return;
  if (sky.fov() > 60) return sky.setBlack(18);
  const [r, d] = sky.center();
  try {
    const c = await renderCutout(SURVEYS.spherex(63), r, d, sky.fov() * 0.8, 24, 16);
    const px = c.getContext("2d").getImageData(0, 0, 24, 16).data, v = [];
    for (let i = 0; i < px.length; i += 4) v.push((px[i] + px[i + 1] + px[i + 2]) / 3);
    v.sort((a, b) => a - b);
    sky.setBlack(v[v.length >> 1] + 6); sky.black = v[v.length >> 1] + 6;
  } catch (e) {}
}
let spinning = false;
function homeView() { return [0, 0, sky.homeFov()]; }
function fitBlend() {                              // cover exactly the area outside the all-sky ellipse (measured, not assumed)
  const W = sky.W, H = sky.H, edge = (dx, dy) => {   // distance from the centre to the map's edge along (dx, dy)
    let lo = 0, hi = Math.hypot(W, H);
    for (let i = 0; i < 22; i++) { const m = (lo + hi) / 2; sky.pix2world(W / 2 + dx * m, H / 2 + dy * m) ? (lo = m) : (hi = m); }
    return lo;
  };
  const w = edge(1, 0) - 0.5, h = edge(0, 1) - 0.5;
  const m = `radial-gradient(ellipse ${w}px ${h}px at 50% 50%, transparent calc(100% - 1.5px), #000 100%)`;
  const b = $("blend"); b.style.webkitMaskImage = b.style.maskImage = m;
}
async function boot() {
  loadCatalog();
  await sky.init();
  sky.set(0, 0, sky.homeFov()); fitBlend();
  sky.startSpin(2.2); spinning = true;
  bindSky();
  window.addEventListener("resize", () => { fitBlend(); if (view === "home") sky.a.setFoV(sky.homeFov()); layoutField(); });
  window.__sky = sky; window.__ready = true;
}

function wake() {                                  // first touch: the universe stops, faint grid shows, zoom works
  if (spinning) { sky.haltSpin(); spinning = false; }
  sky.grid(true);
}
const sepDeg = (a, b) => { const u = unit(a[0], a[1]), v = unit(b[0], b[1]); return Math.acos(Math.max(-1, Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))) * 180 / Math.PI; };

function bindSky() {
  const rect = () => clip.getBoundingClientRect();
  clip.addEventListener("pointerdown", () => { smoothId++; smoothCv.classList.remove("on"); }, true);
  window.addEventListener("pointerup", () => { if (view === "home" && sky.fov() < 8) setTimeout(refreshSmooth, 90); });
  const homeOk = () => view === "home" && !busy && !app.classList.contains("searching") && !app.classList.contains("about") && !app.classList.contains("landing");
  // a short hint that follows the cursor the first few seconds it moves over the sky
  let tipUntil = 0, tipDone = false;
  window.addEventListener("pointermove", e => {
    if (!homeOk() || tipDone || !clip.contains(e.target)) { if (tipUntil && now() > tipUntil) { tipEl.classList.remove("on"); tipDone = true; } return; }
    if (!tipUntil) tipUntil = performance.now() + 4500;
    if (performance.now() > tipUntil) { tipEl.classList.remove("on"); tipDone = true; return; }
    tipEl.style.transform = `translate(${e.clientX + 18}px,${e.clientY + 20}px)`; tipEl.classList.add("on");
  });
  let wt = 0;
  clip.addEventListener("wheel", e => {
    if (!homeOk()) return; e.preventDefault(); e.stopPropagation(); wake();
    const dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY;
    const f = Math.min(sky.homeFov(), Math.max(0.02, sky.fov() * Math.exp(dy * 0.004)));   // ~1.5x per wheel notch
    const r = rect(), before = sky.pix2world(e.clientX - r.left, e.clientY - r.top);
    sky.a.setFoV(f); fitBlend();
    const after = sky.pix2world(e.clientX - r.left, e.clientY - r.top), c = sky.center();
    if (before && after && f < 60) sky.a.gotoRaDec(c[0] + (before[0] - after[0]), Math.max(-90, Math.min(90, c[1] + before[1] - after[1])));   // zoom toward the cursor
 clearTimeout(wt); smoothCv.classList.remove("on"); smoothId++; wt = setTimeout(async () => { await autoCuts(); refreshSmooth();  }, 110); }, { capture: true, passive: false });
  clip.addEventListener("contextmenu", e => { e.preventDefault(); e.stopPropagation(); }, true);
  window.addEventListener("contextmenu", e => { if (clip.contains(e.target)) { e.preventDefault(); e.stopPropagation(); } }, true);
  // Right-click drag: draw a smooth circular selection area where the anchor point is the top-left of the circle
  let sel = null;
  const stop = e => { e.stopPropagation(); e.preventDefault(); };
  const begin = e => {
    if (e.button !== 2) { if (homeOk() && e.button === 0) wake(); return; }
    stop(e); if (!homeOk() || sel) return;
    wake(); const r = rect();
    const x0 = e.clientX - r.left, y0 = e.clientY - r.top;
    sel = { x0, y0, cx: x0, cy: y0, r: 0 };
    drawSel(); selEl.classList.remove("fading"); selEl.classList.add("on"); tipDone = true; tipEl.classList.remove("on");
  };
  clip.addEventListener("pointerdown", begin, true);
  clip.addEventListener("mousedown", e => { if (e.button === 2) stop(e); }, true);

  const drawSel = () => {
    if (!sel) return;
    const d = Math.round(sel.r * 2);
    Object.assign(selEl.style, {
      left: `${sel.cx}px`,
      top: `${sel.cy}px`,
      width: `${d}px`,
      height: `${d}px`
    });
  };

  window.addEventListener("pointermove", e => {
    if (!sel) return; stop(e);
    const r = rect();
    const currX = e.clientX - r.left, currY = e.clientY - r.top;
    const dx = currX - sel.x0, dy = currY - sel.y0;
    const sx = dx >= 0 ? 1 : -1, sy = dy >= 0 ? 1 : -1;
    const rawDist = Math.max(Math.abs(dx), Math.abs(dy));
    // Insensitive smooth stretching: dampened with exponent 0.90
    const effectiveD = rawDist < 6 ? rawDist * 0.5 : 8 + Math.pow(Math.max(0, rawDist - 6), 0.90) * 1.15;
    const D = Math.max(16, Math.min(Math.min(sky.W, sky.H) * 0.9, effectiveD));
    const xLeft = sx > 0 ? sel.x0 : sel.x0 - D;
    const yTop = sy > 0 ? sel.y0 : sel.y0 - D;
    sel.cx = xLeft + D / 2;
    sel.cy = yTop + D / 2;
    sel.r = D / 2;
    drawSel();
  }, true);
  window.addEventListener("mousemove", e => { if (sel) stop(e); }, true);
  window.addEventListener("mouseup", e => { if (sel && e.button === 2) stop(e); }, true);
  window.addEventListener("pointerup", async e => {
    if (!sel || e.button !== 2) return; stop(e);
    const s0 = sel; sel = null;
    if (s0.r < 14) { selEl.classList.remove("on"); return; }
    const cx = s0.cx, cy = s0.cy, rad = s0.r;

    const c = sky.pix2world(cx, cy);
    const topPt = sky.pix2world(cx, cy - rad);
    const botPt = sky.pix2world(cx, cy + rad);
    if (!c || !topPt || !botPt) { selEl.classList.remove("on"); return toast("Keep the selection circle inside the map."); }

    // Diameter of selected circular region on the sky in arcseconds
    const side = sepDeg(topPt, botPt) * 3600;
    if (side > 7200) { selEl.classList.remove("on"); return toast(`That circle is ${(side / 3600).toFixed(side > 36000 ? 0 : 1)}° across. Scroll to zoom in and pick up to 2°.`); }
    
    // Smooth transition into the background
    selEl.classList.add("fading");
    setTimeout(() => {
      selEl.classList.remove("on", "fading");
    }, 850);

    // Auto-snap to the brightest star / object within the circle by sampling HiPS pixels
    let targetRa = c[0], targetDec = c[1];
    const raStr = `${c[0].toFixed(2)}°`;
    const decStr = `${c[1] >= 0 ? "+" : ""}${c[1].toFixed(2)}°`;
    let targetName = `Selected Region (${raStr}, ${decStr})`;
    try {
      const sw = 48, sh = 48;
      const cutout = await renderCutout(SURVEYS.tmass, c[0], c[1], side / 3600, sw, sh, {});
      const data = cutout.getContext("2d").getImageData(0, 0, sw, sh).data;
      let maxB = 0, bestX = sw / 2, bestY = sh / 2;
      for (let py = 0; py < sh; py++) {
        for (let px = 0; px < sw; px++) {
          // Check circular mask within the square cutout
          const ndx = px - sw / 2, ndy = py - sh / 2;
          if (Math.hypot(ndx, ndy) > sw / 2) continue;
          const idx = (py * sw + px) * 4;
          const b = data[idx] + data[idx + 1] + data[idx + 2];
          if (b > maxB) { maxB = b; bestX = px; bestY = py; }
        }
      }
      if (maxB > 60) {
        const fracX = (bestX + 0.5 - sw / 2) / sw, fracY = (sh / 2 - (bestY + 0.5)) / sh;
        const degW = side / 3600;
        const dRa = -fracX * degW / Math.cos(c[1] * Math.PI / 180), dDec = fracY * degW;
        targetRa = ((c[0] + dRa) % 360 + 360) % 360;
        targetDec = Math.max(-90, Math.min(90, c[1] + dDec));
        targetName = `Selected Target (${targetRa.toFixed(2)}°, ${targetDec >= 0 ? "+" : ""}${targetDec.toFixed(2)}°)`;
      }
    } catch (e) {
      const boxRadiusDeg = Math.max(side / 7200, 0.08);
      const catList = (CATALOG && CATALOG.length) ? CATALOG : FAMOUS;
      if (catList && catList.length) {
        let best = null, bestDist = boxRadiusDeg;
        for (const obj of catList) {
          const d = sepDeg([obj.ra, obj.dec], c);
          if (d < bestDist) { bestDist = d; best = obj; }
        }
        if (best) { targetRa = best.ra; targetDec = best.dec; targetName = best.name; }
      }
    }
    go({ key: "custom", name: targetName, ra: targetRa, dec: targetDec, pmra: 0, pmdec: 0, epoch: args.mid, fov: Math.max(60, side) });
  }, true);
}

/* ---------------------------------------------------------------- flight */
const panelA = () => { const r = $("pSpx").getBoundingClientRect(); return r.width ? r.height / r.width : 1.4; };
// width of the field on screen at the end of the flight; the panel window (width x width*a) must fit inside the viewport
const ENDFOV = () => req.fieldDeg * sky.W / (SF() * panelA());   // sky width so the field spans the panel's height
const SF = () => Math.min(sky.W * 0.4, 0.8 * (sky.H - 20) / panelA());
let nonce = 0;

const OFF = [0.13, -0.17];                           // where the target waits during the zoom, as a fraction of the view (x right, y up)
function offsetOf(c, t) {                            // target relative to the view centre, degrees on the sky (x right = west, y up = north)
  const R = Math.PI / 180, a0 = c[0] * R, d0 = c[1] * R, a = t[0] * R, d = t[1] * R;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a - a0);
  const xi = Math.cos(d) * Math.sin(a - a0) / cosc, eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a - a0)) / cosc;
  return cosc > 0 ? [-xi / R, eta / R] : [180, 0];
}
function centreFor(t, off) {                         // centre that puts the target at screen offset off (deg): a true step on the sphere
  const R = Math.PI / 180, xi = off[0] * R, eta = -off[1] * R, a0 = t[0] * R, d0 = t[1] * R, rho = Math.hypot(xi, eta);
  if (rho < 1e-12) return [t[0], t[1]];
  const c = Math.atan(rho), sc = Math.sin(c), cc = Math.cos(c);
  const dec = Math.asin(cc * Math.sin(d0) + eta * sc * Math.cos(d0) / rho);
  const ra = a0 + Math.atan2(xi * sc, rho * Math.cos(d0) * cc - eta * Math.sin(d0) * sc);
  return [((ra / R) % 360 + 360) % 360, dec / R];
}
/* ---------------------------------------------------------------- target lock-on reticle & cinematic spaceflight */
const warpCv = $("warpCv"), beaconEl = $("beacon");
const warpCtx = warpCv ? warpCv.getContext("2d") : null;
let lastTargetScreen = null;

function updateLockAndWarp(ra, dec, t, label) {
  const W = sky.W || window.innerWidth, H = sky.H || window.innerHeight;
  let cx = W / 2, cy = H / 2, onScreen = false;
  try {
    const p = sky.world2pix(ra, dec);
    if (p && Number.isFinite(p[0]) && Number.isFinite(p[1]) && p[0] > -120 && p[0] < W + 120 && p[1] > -120 && p[1] < H + 120) {
      cx = p[0]; cy = p[1]; onScreen = true;
    }
  } catch (e) {}
  lastTargetScreen = [cx, cy];

  if (beaconEl) {
    beaconEl.style.left = `${cx}px`;
    beaconEl.style.top = `${cy}px`;

    // Stage 1: Pan & Track (0.0 to 0.45)
    // Stage 2: Identify & Lock (0.45 to 0.70)
    // Stage 3: High-speed Zoom dive (0.70 to 1.0)
    let opacity = 0;
    if (onScreen) {
      opacity = 1;
    } else if (t > 0.25) {
      opacity = clamp01((t - 0.25) * 4);
    }
    beaconEl.style.opacity = String(opacity);

    const ring = beaconEl.querySelector(".lock-ring");
    if (ring) {
      // Reticle shrinks into a sharp precise lock as camera aligns
      let scale = 1.6;
      if (t < 0.45) {
        scale = 1.6 - 0.4 * smooth(t / 0.45);
      } else if (t < 0.75) {
        scale = 1.2 - 0.2 * smooth((t - 0.45) / 0.3);
      } else {
        scale = 1.0;
      }
      ring.style.transform = `scale(${scale.toFixed(3)})`;
    }

    const lab = beaconEl.querySelector(".lock-label");
    if (lab) {
      // Dynamic readout: show identification status
      let text = label || "Acquiring Coordinates...";
      if (t < 0.35) {
        lab.textContent = `TARGETING: ${text}`;
      } else if (t < 0.75) {
        lab.textContent = `LOCKED: ${text}`;
      } else {
        lab.textContent = text;
      }
    }
  }
}

function clearLockAndWarp() {
  if (warpCv && warpCtx) {
    warpCv.classList.remove("on");
    warpCtx.clearRect(0, 0, warpCv.width, warpCv.height);
  }
  if (beaconEl) {
    beaconEl.style.left = "50%";
    beaconEl.style.top = "50%";
    beaconEl.style.opacity = "";
    const ring = beaconEl.querySelector(".lock-ring");
    if (ring) ring.style.transform = "";
  }
}

async function flyTo(ra, dec, fov, dur, label = "") {   // turn to the target, then zoom in while it glides diagonally to the centre, never leaving the view
  const T = [ra, dec], c0 = sky.center(), f0 = sky.fov(), aspect = sky.H / sky.W;
  const start = offsetOf(c0, T), startFrac = [start[0] / f0, start[1] / f0];
  const visible = Math.abs(startFrac[0]) < 0.42 && Math.abs(startFrac[1]) < 0.42 * aspect && f0 < 200;
  const fA0 = visible ? f0 : Math.max(fov, Math.min(f0 * 0.55, 90));
  const kA = visible ? startFrac : OFF;
  const tA = visible ? 0 : 0.5;                     // share of the time spent turning the globe
    const fPeak = Math.min(sky.homeFov(), Math.max(f0, fA0, Math.hypot(...offsetOf(c0, T)) * 1.7));   // wide enough to see where we are going
  const fA = visible ? f0 : Math.max(fA0, Math.sqrt(fPeak * fov));    // phase 1 ends halfway (in zoom) between the wide view and the target
  const u0 = unit(c0[0], c0[1]), cA = centreFor(T, [kA[0] * fA, kA[1] * fA]), uA = unit(cA[0], cA[1]);
  await tween(dur, t => {
    if (t < tA) {                                    // phase 1: ease out to a wide view, turn, then come back in
      const q = t / tA, ss = x => x * x * x * (x * (x * 6 - 15) + 10);
      const p = ss(clamp01((q - 0.12) / 0.88)), c = radec(slerp(u0, uA, p));          // the globe starts turning just after the zoom-out begins
      const out = ss(clamp01(q / 0.45));                                               // ~1.7 s to widen, not a jump
      const fOut = Math.exp(Math.log(f0) + (Math.log(fPeak) - Math.log(f0)) * out);
      const back = Math.exp(Math.log(fPeak) + (Math.log(fA) - Math.log(fPeak)) * ss(clamp01((q - 0.45) / 0.55)));
      const f = q < 0.45 ? fOut : Math.max(back, Math.hypot(...offsetOf(c, T)) * 3.4);
      sky.set(c[0], c[1], Math.min(f, sky.homeFov()));
    } else {                                         // phase 2: zoom in, the target glides diagonally to the centre
      const u = clamp01((t - tA) / (1 - tA)), z = smooth(u), k = 1 - smooth(Math.min(1, u * 1.08));
      const f = Math.exp(Math.log(fA) + (Math.log(fov) - Math.log(fA)) * z);
      const c = centreFor(T, [kA[0] * k * f, kA[1] * k * f]);
      sky.set(c[0], c[1], f);
    }
    if (overlay) overlay.style.opacity = String(smooth(clamp01((t - 0.88) / 0.12)));
  });
  sky.set(ra, dec, fov);
}
async function flyHome(ra, dec, fov, dur) {         // cinematic pull-back & zoom out to the whole celestial sphere
  const c0 = sky.center(), u0 = unit(c0[0], c0[1]), u1 = unit(ra, dec);
  const f0 = Math.log(sky.fov()), f1 = Math.log(fov);
  
  // Custom 5th order smoother for silky space pull-back (quintic ease-in-out)
  const qsmooth = x => x * x * x * (x * (x * 6 - 15) + 10);
  
  await tween(dur, t => {
    const p = qsmooth(t);
    // Smooth spherical slerp from target direction back to home orientation
    const [r, d] = radec(slerp(u0, u1, p));
    // Exponential logarithmic zoom expansion
    const curFov = Math.exp(f0 + (f1 - f0) * p);
    sky.set(r, d, curFov);
  });
  sky.set(ra, dec, fov);
}

function makeOverlay() {
  if (overlay) overlay.remove();
  overlay = document.createElement("canvas");
  overlay.id = "fw";
  Object.assign(overlay.style, { position: "absolute", inset: "0", width: "100%", height: "100%", opacity: "0", pointerEvents: "none" });
  inner.appendChild(overlay);
}

async function go(item) {
  if (busy && view !== "view") return;
  if (app.classList.contains("deep")) exitDepth();
  closeSearch(true); closeAbout();
  if (view === "view") {                           // from the three panels: dim them away, the sky fades in behind, then fly on
    busy = true; stopPlayer(); setView("leaving");
    clip.style.transition = "none"; clip.style.opacity = "0";
    clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
    app.classList.add("dimming");
    await new Promise(r => setTimeout(r, 30));
    clip.style.transition = "opacity .9s ease"; clip.style.opacity = "1";       // the sky where we are, full screen, under the fading panels
    app.classList.remove("shown");
    await new Promise(r => setTimeout(r, 950));
    app.classList.remove("dimming"); clip.style.transition = "";
    if (overlay) { const o = overlay; overlay = null; o.style.transition = "opacity .6s"; o.style.opacity = "0"; setTimeout(() => o.remove(), 700); }
    sky.grid(true);
  }
  for (const id of ["wiseCv", "spxCv"]) { const c = $(id); c.getContext("2d").clearRect(0, 0, c.width, c.height); }   // no leftovers from the last target
  $("spxCv").classList.remove("on");
  busy = true;
  req = Object.assign({}, item, { nonce: String(Date.now()) + Math.random().toString(36).slice(2, 6) });
  const cmpI = compareFor(item.detector);
  const shift = Math.hypot(item.pmra || 0, item.pmdec || 0) / 1000 * (args.mid - cmpI.epoch);   // arcsec moved since the archive image
  req.pos = propagate(item, cmpI.epoch);                     // archive-image position = centre of both panels
  req.fieldDeg = fieldDeg(Math.max(item.fov || 180, FRAME_FACTOR * shift + 60));
  emit(Object.assign({ type: "go" }, req));
  setView("flying"); sky.haltSpin(); spinning = false; smoothId++; smoothCv.classList.remove("on");
  wiseCanvas = null; skyCanvas = null; makeOverlay(); app.classList.remove("smoothready");
  const my = req.nonce, det = item.detector || "SPHEREx-D3";
  // 1) HiDPI 2MASS map of the whole window, razor-sharp, to take over from Aladin's pixels as the zoom lands
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cw = Math.min(2400, Math.round(sky.W * dpr)), ch = Math.round(cw * sky.H / sky.W);
  renderCutout(SURVEYS.tmass8, req.pos[0], req.pos[1], ENDFOV(), cw, ch, { gain: 1.16, gamma: 0.92, black: 0.02 }).then(c => {
    if (!req || req.nonce !== my) return;
    skyCanvas = c; overlay.width = c.width; overlay.height = c.height; overlay.getContext("2d").drawImage(c, 0, 0);
    app.classList.add("smoothready"); if (req.arrived) overlay.style.opacity = "1";
  }).catch(() => {});
  // 2) the archive image at the same wavelength, same window, for the left panel
  const f = $("pSpx").getBoundingClientRect(), pw = Math.min(840, Math.round(f.width * dpr)), ph = Math.round(pw * f.height / f.width);
  req.wiseJob = { survey: cmpI.survey, pw, ph };
  // the server builds the SPHEREx field with exactly this centre and size, so the AllWISE picture can start now
  req.wiseJob.p = renderCutout(req.wiseJob.survey, req.pos[0], req.pos[1], req.fieldDeg * pw / ph, pw, ph, WISETONE).catch(() => null);
  startProgressBar();
  const targetLabel = item.name || (item.ra != null && item.dec != null ? `${Number(item.ra).toFixed(2)}°, ${Number(item.dec) >= 0 ? "+" : ""}${Number(item.dec).toFixed(2)}°` : "Target");
  await flyTo(req.pos[0], req.pos[1], ENDFOV(), 7500, targetLabel);     // the slow, readable flight
  req.arrived = true; autoCuts(); if (skyCanvas) overlay.style.opacity = "1";

  setView("waiting"); tryReveal();
}

let progInterval = null;
function startProgressBar() {
  stopProgressBar();
  const bar = $("prog-bar");
  if (!bar) return;
  bar.style.width = "0%";
  let progress = 0;
  const startT = performance.now();
  const duration = 7400;  // fills while the 7.5 s flight runs
  progInterval = setInterval(() => {
    const elapsed = performance.now() - startT;
    const t = Math.min(1, elapsed / duration);
    // Smooth ease-out ramp to 97%
    progress = smooth(t) * 97;
    bar.style.width = Math.min(progress, 98) + "%";
    if (t >= 1) { clearInterval(progInterval); progInterval = null; }
  }, 30);
}
function stopProgressBar(complete = false) {
  if (progInterval) { clearInterval(progInterval); progInterval = null; }
  const bar = $("prog-bar");
  if (!bar) return;
  if (complete) {
    bar.style.width = "100%";
    setTimeout(() => { bar.style.width = "0%"; }, 350);
  } else {
    bar.style.width = "0%";
  }
}

/* ---------------------------------------------------------------- reveal (squish into the SPHEREx panel) */
function layoutField() {}
const geom = () => {
  const W = sky.W, H = sky.H, f = $("pSpx").getBoundingClientRect(), S = SF();
  return { W, H, f, S, k: f.width / S, A: f.height / f.width, dx: f.left + f.width / 2 - W / 2, dy: f.top + f.height / 2 - H / 2 };
};
async function tryReveal() {
  const res = args.result;
  if (!req || !req.arrived || view === "view" || view === "settling") return;   // never cut the flight short
  if (args.error) { stopProgressBar(false); toast(args.error); return abortHome(); }
  if (!res || res.nonce !== req.nonce) return;
  stopProgressBar(true);
  setView("settling");
  fillPage(res);
  loadFrames(res);
  const wisePromise = (async () => {
    try {
      const j = req.wiseJob, same = Math.abs(res.field_deg / req.fieldDeg - 1) < 0.02 &&
        Math.hypot(...offsetOf([res.ra_c, res.dec_c], req.pos)) * 3600 < 6;
      const job = same ? j.p : renderCutout(j.survey, res.ra_c, res.dec_c, res.field_deg * j.pw / j.ph, j.pw, j.ph, WISETONE);
      return await job;
    } catch (e) { return null; }
  })();

  const { W, H, S, k, A, dx, dy } = geom();
  preparePlayer(res);                                       // frame 1 drawn underneath, invisible
  setTimeout(() => app.classList.add("shown"), 150);

  clip.style.willChange = "clip-path, transform";
  inner.style.willChange = "transform";
  clip.style.backfaceVisibility = "hidden";
  inner.style.backfaceVisibility = "hidden";
  const tweenPromise = tween(650, t => {
    const p = smooth(t), w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
    inner.style.transform = `translate3d(${dx * p}px,${dy * p}px,0) scale(${1 + (k - 1) * p})`;
    clip.style.clipPath = `inset(${Math.max(0, cy - h / 2)}px ${Math.max(0, W - cx - w / 2)}px ${Math.max(0, H - cy - h / 2)}px ${Math.max(0, cx - w / 2)}px round ${26 * smooth(clamp01(p * 1.3))}px)`;
  });

  await tweenPromise;
  clip.style.willChange = "auto";
  inner.style.willChange = "auto";
  clearLockAndWarp();
  // the archive picture arrives when it arrives: paint now (empty if not ready) and again once it is in
  wiseCanvas = null; paintWise(res);
  wisePromise.then(c => { if (c && args.result && args.result.nonce === res.nonce) { wiseCanvas = c; paintWise(res); } });
  $("spxCv").classList.add("on");                           // the map hands over to the real SPHEREx frames
  await new Promise(r => setTimeout(r, 150));
  clip.style.visibility = "hidden";
  setView("view"); startPlayer();
  syncCmp(res);
  if (res.bands) onBands(res);
  else emit({ type: "bands", nonce: "b" + Date.now(), for: res.nonce, key: req && req.key });
}

function abortHome() { clearLockAndWarp(); busy = false; req = null; leave(true); }

async function leave(fromError = false) {
  if (busy && !fromError && view !== "view" && view !== "waiting") return;
  if (app.classList.contains("deep")) exitDepth();
  stopPlayer(); clearLockAndWarp();
  const was = view;
  emit({ type: "home", nonce: "h" + Date.now() });
  setView("leaving");
  app.classList.remove("shown");
  selEl.classList.remove("on");
  if (was === "view" || was === "settling") await unsquish();
  clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
  if (overlay) { const o = overlay; overlay = null; o.style.transition = "opacity .6s"; o.style.opacity = "0"; setTimeout(() => o.remove(), 650); }
  setView("flying");
  const c = sky.center();
  // Cinematic 2.4s pull-back and zoom out with graceful galactic deceleration
  await flyHome(c[0], 0, sky.homeFov(), 2400);
  clearLockAndWarp();
  setView("home"); fitBlend(); busy = false; req = null; sky.grid(false); autoCuts(); app.classList.remove("zoomed"); sky.startSpin(2.2); spinning = true;
}
// the three panels fold back into the full-screen sky (reverse of the reveal)
async function unsquish() {
  {
    const { W, H, S, k, A, dx, dy } = geom();
    clip.style.visibility = "visible"; $("spxCv").classList.remove("on");
    await new Promise(r => setTimeout(r, 120));
    clip.style.willChange = "clip-path, transform";
    inner.style.willChange = "transform";
    await tween(700, t => {
      const p = 1 - smooth(t), w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
      inner.style.transform = `translate3d(${dx * p}px,${dy * p}px,0) scale(${1 + (k - 1) * p})`;
      const g = smooth(t);
      clip.style.clipPath = `inset(${(cy - h / 2) * (1 - g)}px ${(W - cx - w / 2) * (1 - g)}px ${(H - cy - h / 2) * (1 - g)}px ${(cx - w / 2) * (1 - g)}px round ${26 * (1 - t)}px)`;
    });
    clip.style.willChange = "auto";
    inner.style.willChange = "auto";
  }
  clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
}

/* ---------------------------------------------------------------- page two content */
const hasPM = res => Math.hypot(res.pmra || 0, res.pmdec || 0) > 50;     // a ring only helps for real movers
const fmtN = (x, d = 0) => x.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
function fmtAgo(y) {                                 // how long ago light left, in plain units
  if (y == null) return null;
  if (y < 1000) return `${fmtN(y, y < 10 ? 1 : 0)} yrs`;
  if (y < 1e6) return `${fmtN(Math.round(y / 100) * 100)} yrs`;
  if (y < 1e9) return `${fmtN(y / 1e6, y < 1e7 ? 1 : 0)} Myr`;
  return `${fmtN(y / 1e9, 1)} Gyr`;
}
function fmtLy(ly) { return ly == null ? null : ly < 100 ? `${fmtN(ly, 1)} ly` : `${fmtN(ly)} ly`; }
function fact(v, label) { return `<div><b>${v ?? "—"}</b><span>${label}</span></div>`; }
function fillPage(res) {
  last_res = res; specFor = "target"; bandSel = detNum(res.detector);
  const f = res.facts || {};
  $("iName").textContent = res.name;
  const where = f.constellation ? `in ${f.constellation}` : "";
  $("iKind").textContent = f.kind ? `${f.kind}${where ? " · " + where : ""}` : `Patch of sky ${where}`.trim();
  let row;
  if (!f.kind) row = fact(`${fmtN(f.field_arcmin || res.field_deg * 60, 1)}′`, "field width") + fact((res.objects || []).filter(o => inView(res, o)).length, "objects listed") + fact(res.visits, "SPHEREx visits");
  else if (f.galaxy) row = fact(f.redshift != null ? `z ${fmtN(f.redshift, Math.abs(f.redshift) < 0.1 ? 4 : 3)}` : null, f.redshift < 0 ? "moving toward us" : "redshift") + fact(fmtAgo(f.light_years_ago), "light left") + fact(f.size_arcmin ? `${fmtN(f.size_arcmin, 1)}′` : null, "size on sky");
  else row = fact(fmtLy(f.distance_ly), "distance") + fact(f.temp_k ? `≈${fmtN(f.temp_k)} K` : null, "surface temp") + fact(f.motion_arcsec_yr != null ? `${fmtN(f.motion_arcsec_yr, f.motion_arcsec_yr < 1 ? 2 : 1)}″/yr` : null, "motion on sky");
  $("iFacts").innerHTML = row;
  const [a, b] = sexa(res.ra, res.dec);
  $("iCoord").textContent = `${a}   ${b}`;
  $("iSrc").textContent = res.visits ? `${res.visits} SPHEREx visits · ${monthYr(res.span?.[0])} – ${monthYr(res.span?.[1])}` : "";
  const lc = res.lc;
  $("iLc").innerHTML = lc ? `${lc.pts && lc.pts.length ? sparkline(lc.pts) : ""}<span>${lc.word}</span>` : `<span class="dim">Too few clean exposures to tell</span>`;
  $("wiseH").textContent = res.compare.name;
  $("wiseCap").textContent = res.compare.year || "2010";
  fillObjects(res);
  drawSpectrum(); buildWave(res);
}
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthYr = s => (s ? `${MON[+s.slice(5, 7) - 1]} ${s.slice(0, 4)}` : "");
function sparkline(pts) {
  const W = 120, H = 26, t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const vs = pts.map(p => p[1]), lo = Math.min(0.85, ...vs), hi = Math.max(1.15, ...vs);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${((p[0] - t0) / (t1 - t0 || 1) * W).toFixed(1)},${(H - (p[1] - lo) / (hi - lo) * H).toFixed(1)}`).join("");
  const y1 = (H - (1 - lo) / (hi - lo) * H).toFixed(1);
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><line x1="0" x2="${W}" y1="${y1}" y2="${y1}" class="base"/><path d="${d}"/></svg>`;
}

/* ---------------------------------------------------------------- spectrum */
let specFor = "target";
const BAND_RANGE = { 1: [0.75, 1.11], 2: [1.11, 1.64], 3: [1.64, 2.42], 4: [2.42, 3.82], 5: [3.82, 4.42], 6: [4.42, 5.0] };
function drawSpectrum() {
  const res = last_res, el = $("iSpec"); if (!res) return;
  const objs = res.objects || [], nm = o => o ? (o.name || "unnamed source") : "the centre";
  const who = specFor === "target" ? (res.facts?.kind ? res.name : nm(objs[0])) : nm(objs[+specFor]);
  let key = specFor;
  if (specFor === "target" && !res.facts?.kind && objs.length) key = "0";       // a patch of sky: its brightest listed object
  $("specH").textContent = `Spectrum · ${who || ""}`;
  if (!res.spectra) { el.innerHTML = `<div class="wait">Measuring all 102 colours across six detectors…</div>`; return; }
  const sp = res.spectra[key];
  if (!sp || sp.smooth.length < 3) { el.innerHTML = `<div class="wait">Too faint or too close to the edge for a spectrum</div>`; return; }
  const W = 300, H = 118, L = 30, R = 6, T = 14, B = 18, x = w => L + (w - 0.75) / (5 - 0.75) * (W - L - R);
  const ys = sp.smooth.flatMap(s => [s[2], s[3]]).concat(sp.pts.map(p => p[1])).filter(v => v > 0).sort((a, b) => a - b);
  const lo = ys[Math.floor(ys.length * 0.02)] || 1, hi = ys[Math.floor(ys.length * 0.98)] || 10, useLog = hi / lo > 25;
  const fy = useLog ? v => Math.log10(Math.max(v, lo / 2)) : v => v, a0 = fy(useLog ? lo / 1.3 : Math.min(0, lo)), a1 = fy(hi * 1.12);
  const y = v => T + (1 - (fy(v) - a0) / (a1 - a0)) * (H - T - B);
  const r = BAND_RANGE[bandSel] || [0, 0];
  let g = `<rect x="${x(r[0])}" y="${T}" width="${x(r[1]) - x(r[0])}" height="${H - T - B}" class="band"/>`;
  for (const w of [1, 2, 3, 4]) g += `<text x="${x(w)}" y="${H - 4}" class="ax">${w}</text>`;
  g += `<text x="${W - R}" y="${H - 4}" class="ax end">5 µm</text>`;
  g += sp.pts.map(p => `<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="1.3" class="pt"/>`).join("");
  const top = sp.smooth.map((s, i) => `${i ? "L" : "M"}${x(s[0]).toFixed(1)},${y(s[3]).toFixed(1)}`).join("");
  const bot = sp.smooth.slice().reverse().map(s => `L${x(s[0]).toFixed(1)},${y(Math.max(s[2], lo / 2)).toFixed(1)}`).join("");
  g += `<path d="${top}${bot}Z" class="err"/>`;
  g += `<path d="${sp.smooth.map((s, i) => `${i ? "L" : "M"}${x(s[0]).toFixed(1)},${y(s[1]).toFixed(1)}`).join("")}" class="line"/>`;
  for (const f of sp.features) g += `<line x1="${x(f.wl)}" x2="${x(f.wl)}" y1="${T}" y2="${H - B}" class="ft"/><text x="${x(f.wl)}" y="${T - 3}" class="ftl">${f.label}</text>`;
  const note = sp.features.length ? "" : `<p class="specnote">No strong absorption features</p>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Spectrum of ${who}">${g}</svg>${note}${sp.wings ? `<p class="specnote">Saturated core: shape measured in the star's wings</p>` : ""}`;
}

/* ---------------------------------------------------------------- wavelength bar (bottom) */
let bandSel = 3;
function buildWave(res) {
  const segs = $("waveSegs"); segs.innerHTML = "";
  const have = new Set((res.bands || []).filter(b => b.ok).map(b => b.det)); have.add(detNum(res.detector));
  for (let d = 1; d <= 6; d++) {
    const [a, b] = BAND_RANGE[d], s = document.createElement("button");
    s.type = "button"; s.style.flex = String(b - a); s.dataset.d = d;
    s.className = (have.has(d) ? "" : "off") + (d === bandSel ? " on" : "");
    s.innerHTML = `<span>${a.toFixed(2)}–${b.toFixed(2)}</span>`;
    s.setAttribute("aria-label", `${a}–${b} micrometres`);
    s.onclick = () => selectBand(d);
    segs.appendChild(s);
  }
  placeKnob(); waveLabel();
}
const BAND_NOTE = { 1: "starlight; hot stars shine brightest", 2: "water vapour dips in cool stars", 3: "sees through dust", 4: "water ice and glowing dust", 5: "carbon dioxide ice", 6: "carbon monoxide ice, warm dust" };
function waveLabel() {
  const [a, b] = BAND_RANGE[bandSel], ready = bandReady(bandSel);
  $("waveLab").innerHTML = `<b>${a.toFixed(2)}–${b.toFixed(2)} µm</b> · ${BAND_NOTE[bandSel]}${ready ? "" : ` <em>loading…</em>`}`;
  $("wave").setAttribute("aria-valuenow", bandSel);
}
const bandReady = d => !last_res || d === detNum(last_res.detector) || (last_res.bands || []).some(b => b.det === d && b.ok);
function placeKnob() {
  const seg = $("waveSegs").querySelector(`[data-d="${bandSel}"]`), w = $("wave"); if (!seg) return;
  const r = seg.getBoundingClientRect(), r0 = w.getBoundingClientRect();
  $("waveKnob").style.left = `${r.left - r0.left + r.width / 2}px`;
  [...$("waveSegs").children].forEach(c => c.classList.toggle("on", +c.dataset.d === bandSel));
}
function selectBand(d) {
  if (!last_res || d === bandSel) return;
  bandSel = d; placeKnob(); waveLabel(); drawSpectrum();
  if (bandReady(d)) showBand(d);
}
const dateYear = s => { const t = Date.parse(s + "T00:00:00Z"); return 1970 + t / 31557600000; };
function showBand(d) {
  const res = last_res; if (!res || !P) return;
  const base = d === detNum(res.detector), b = (res.bands || []).find(x => x.det === d);
  const src = base ? res : b; if (!src) return;
  loadFrames({ frames: src.frames }).then(() => { P.curDates = src.dates; P.curYears = base ? res.years : src.dates.map(dateYear); P.curBase = base; tpos = 0; drawSpx(); });
}
(() => {                                             // drag along the bar, or use the arrow keys
  const w = $("wave"); let down = false;
  const pick = e => { const segs = [...$("waveSegs").children]; for (const s of segs) { const r = s.getBoundingClientRect(); if (e.clientX >= r.left && e.clientX <= r.right) return selectBand(+s.dataset.d); } };
  w.addEventListener("pointerdown", e => { down = true; w.setPointerCapture(e.pointerId); pick(e); });
  w.addEventListener("pointermove", e => { if (down) pick(e); });
  w.addEventListener("pointerup", () => { down = false; });
  w.addEventListener("keydown", e => { if (e.key === "ArrowRight" && bandSel < 6) selectBand(bandSel + 1); if (e.key === "ArrowLeft" && bandSel > 1) selectBand(bandSel - 1); });
  window.addEventListener("resize", placeKnob);
})();
function onBands(res) {                             // the other five ranges and the spectra have arrived
  last_res = Object.assign(last_res || {}, { bands: res.bands, spectra: res.spectra });
  buildWave(last_res); drawSpectrum();
  if (bandSel !== detNum(last_res.detector) && bandReady(bandSel)) showBand(bandSel);
}

/* "In the frame": a small picture of each object and what it is. Hover rings it in both panels. */
let hl = null;
// is this object inside the part of the field the image panels show? (a tall panel crops the field's sides)
function inView(res, o) {
  const f = $("pSpx").getBoundingClientRect(); if (!f.width) return true;
  const p = toPx(res, o.ra, o.dec, f.width, f.height), m = 6;
  return p[0] >= -m && p[0] <= f.width + m && p[1] >= -m && p[1] <= f.height + m;
}
function fillObjects(res) {
  const ul = $("iObjs"); ul.innerHTML = ""; hl = null; drawHl();
  const all = res.objects || [], list = all.filter(o => inView(res, o));   // only what the panels actually show
  if (!list.length) { ul.innerHTML = `<li class="none">Nothing bright enough to list</li>`; return; }
  list.forEach(o => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="th"></div><div><div class="nm"></div><div class="kd"></div></div>`;
    li.querySelector(".nm").textContent = o.name || "Unnamed source";
    const ago = fmtAgo(o.ago);
    li.querySelector(".kd").textContent = o.kind + (ago ? ` · light left ${ago} ago` : "") + (o.name ? "" : " (our model's guess)");
    const idx = String(all.indexOf(o));
    li.onmouseenter = () => { hl = o; li.classList.add("on"); drawHl(); specFor = idx; drawSpectrum(); };
    li.onmouseleave = () => { if (hl === o) hl = null; li.classList.remove("on"); drawHl(); specFor = "target"; drawSpectrum(); };
    ul.appendChild(li);
    const fov = Math.min(res.field_deg, Math.max(0.025, 1.5 * (o.size || 0) / 3600));
    objThumbQ.push({ o, box: li.querySelector(".th"), fov });
  });
  pumpObj();
}
// stretch each small picture on its own: a bright star no longer turns the whole square white
function autoTone(c) {
  const g = c.getContext("2d"), im = g.getImageData(0, 0, c.width, c.height), d = im.data, L = [];
  for (let i = 0; i < d.length; i += 4) L.push(d[i] + d[i + 1] + d[i + 2]);
  L.sort((a, b) => a - b);
  const lo = L[Math.floor(L.length * .6)], hi = Math.max(lo + 60, L[Math.floor(L.length * .995)]);
  for (let i = 0; i < d.length; i += 4) {
    const l = d[i] + d[i + 1] + d[i + 2], t = Math.min(1, Math.max(0, (l - lo) / (hi - lo))), q = Math.asinh(t * 6) / Math.asinh(6), k = l > 0 ? q * 765 / l : 0;
    for (let c2 = 0; c2 < 3; c2++) d[i + c2] = Math.min(255, 19 + d[i + c2] * k * .92);
  }
  g.putImageData(im, 0, 0); return c;
}
const objThumbQ = []; let objBusy = false;
async function pumpObj() {
  if (objBusy) return; objBusy = true;
  while (objThumbQ.length) {
    const { o, box, fov } = objThumbQ.shift(); if (!box.isConnected) continue;
    try {
      const c = autoTone(await renderCutout(SURVEYS.tmass, o.ra, o.dec, fov, 88, 88, {}));
      if (box.isConnected) { box.appendChild(c); requestAnimationFrame(() => c.classList.add("ok")); }
    } catch (e) { /* no picture: the row still reads */ }
  }
  objBusy = false;
}
function drawHl() {
  for (const [cid, pid] of [["wiseHl", "pWise"], ["spxHl", "pSpx"]]) {
    const cv = $(cid), f = $(pid).getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(f.width * d)) { cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d); }
    const g = cv.getContext("2d"); g.clearRect(0, 0, cv.width, cv.height);
    const res = P || last_res; if (!hl || !res) continue;
    const yr = cid === "wiseHl" ? (res.compare && res.compare.epoch) || 2010.5 : null;   // where the old survey saw it
    const q = yr && (hl.pmra || hl.pmdec) ? propagate({ ra: hl.ra, dec: hl.dec, pmra: hl.pmra, pmdec: hl.pmdec, epoch: hl.epoch }, yr) : [hl.ra, hl.dec];
    const p = toPx(res, q[0], q[1], cv.width, cv.height);
    const r = Math.max(cv.height * 0.04, cv.height * (hl.size || 0) / 2 / (res.field_deg * 3600));
    g.save(); g.strokeStyle = "rgba(255,255,255,.95)"; g.lineWidth = Math.max(2, cv.height / 220);
    g.shadowColor = "rgba(0,0,0,.8)"; g.shadowBlur = 6;
    g.beginPath(); g.arc(p[0], p[1], r, 0, Math.PI * 2); g.stroke(); g.restore();
  }
}
let last_res = null;
const SPX_PSF = 9.0, WISE_PSF = { W1: 6.1, W2: 6.4, J: 2.9, H: 2.8, K: 2.9 };   // arcsec FWHM
function paintWise(res) {
  const cv = $("wiseCv"), f = $("pWise").getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d);
  if (!cv.width || !cv.height) return;                 // the archive panel is hidden until compared
  const g = cv.getContext("2d"); g.imageSmoothingQuality = "high";
  g.fillStyle = "#13161a"; g.fillRect(0, 0, cv.width, cv.height);
  if (wiseCanvas) {
    // blur WISE to SPHEREx's sharpness so the two panels show the same detail
    const extra = Math.sqrt(Math.max(0, SPX_PSF ** 2 - (WISE_PSF[res.compare.band] || 6.1) ** 2)) / 2.355;
    const pxPerArcsec = cv.height / (res.field_deg * 3600);
    g.filter = `blur(${(extra * pxPerArcsec).toFixed(2)}px)`;
    g.drawImage(wiseCanvas, 0, 0, cv.width, cv.height); g.filter = "none";
    const im = g.getImageData(0, 0, cv.width, cv.height), d = im.data, v = [];
    for (let i = 0; i < d.length; i += 64) v.push(d[i]);
    v.sort((a, b) => a - b);
    const med = v[Math.floor(v.length * 0.5)], sg = Math.max(1, v[Math.floor(v.length * 0.84)] - med);
    let lo = med + 1 * sg, hi = Math.max(lo + 14 * sg, v[Math.floor(v.length * 0.995)]);   // noise stays black
    if (lo > 200) { lo = v[Math.floor(v.length * 0.6)]; hi = 255; }   // a bright star fills the field: stretch only its glow
    for (let i = 0; i < d.length; i += 4) {
      const t = Math.min(1, Math.max(0, (d[i] - lo) / (hi - lo))), q = Math.asinh(t * 8) / Math.asinh(8);
      d[i] = 19 + q * 219; d[i + 1] = 22 + q * 218; d[i + 2] = 26 + q * 218;
    }
    g.putImageData(im, 0, 0);
  }
  for (const m of res.movers || []) {               // every confirmed mover: where AllWISE saw it in 2010
    const p = toPx(res, m.ra0, m.dec0, cv.width, cv.height);
    g.save(); g.strokeStyle = "rgba(200,204,210,.9)"; g.lineWidth = Math.max(1.5, cv.height / 300);
    g.beginPath(); g.arc(p[0], p[1], cv.height * 0.035, 0, Math.PI * 2); g.stroke(); g.restore();
  }
}
// sky -> panel pixels; the field spans the panel height, centred, north up, east left
function toPx(res, ra, dec, cw, ch) {
  const s = res.field_deg * 3600, cd = Math.cos(res.dec_c * Math.PI / 180);
  let dra = ra - res.ra_c; dra = ((dra + 540) % 360) - 180;
  return [cw / 2 - ch * dra * cd * 3600 / s, ch / 2 - ch * (dec - res.dec_c) * 3600 / s];
}
const moverAt = (m, year) => propagate({ ra: m.ra0, dec: m.dec0, pmra: m.pmra, pmdec: m.pmdec, epoch: m.epoch0 }, year);
function ghost(g, x0, y0, x1, y1, size) {           // where it was in 2010 (grey) -> where it is now
  g.save(); g.strokeStyle = "rgba(200,204,210,.85)"; g.lineWidth = Math.max(1.5, size / 300);
  g.beginPath(); g.arc(x0, y0, size * 0.045, 0, Math.PI * 2); g.stroke();
  const d = Math.hypot(x1 - x0, y1 - y0), r0 = size * 0.045, r1 = size * 0.055;
  if (d > r0 + r1) {
    const ux = (x1 - x0) / d, uy = (y1 - y0) / d;
    g.setLineDash([size / 90, size / 110]); g.beginPath();
    g.moveTo(x0 + ux * r0, y0 + uy * r0); g.lineTo(x1 - ux * r1, y1 - uy * r1); g.stroke();
  }
  g.restore();
}
function wisePos(res, cw, ch) {                    // 2010.4 position in panel pixels; the field spans the panel height
  const p = propagate({ ra: res.ra, dec: res.dec, pmra: res.pmra, pmdec: res.pmdec, epoch: args.mid }, 2010.4), s = res.field_deg * 3600;
  const dx = (p[0] - res.ra_c) * Math.cos(res.dec_c * Math.PI / 180) * 3600, dy = (p[1] - res.dec_c) * 3600;
  return [cw / 2 - ch * dx / s, ch / 2 - ch * dy / s];
}
function ring(g, x, y, size, a = 0.85) {
  if (x < -size * .1 || x > size * 1.1) return;
  g.save(); g.strokeStyle = `rgba(190,120,132,${a})`; g.lineWidth = Math.max(2, size / 240);
  g.beginPath(); g.arc(x, y, size * 0.055, 0, Math.PI * 2); g.stroke(); g.restore();
}

/* ---------------------------------------------------------------- SPHEREx player */
let P = null, imgs = [], playing = true, tpos = 0, dragging = false, raf = 0, last = 0, backdrop = null, tmp = null;
let procMode = "normal"; // "normal" | "diff" | "stack"
let stackedCv = null;

function computeStack() {
  if (!imgs.length) return null;
  const c = document.createElement("canvas");
  c.width = imgs[0].naturalWidth || imgs[0].width || 512;
  c.height = imgs[0].naturalHeight || imgs[0].height || 512;
  const ctx = c.getContext("2d");
  const count = imgs.length;
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.globalAlpha = 1.0 / count;
  for (let k = 0; k < count; k++) {
    ctx.drawImage(imgs[k], 0, 0, c.width, c.height);
  }
  ctx.globalAlpha = 1.0;
  return c;
}

function loadFrames(res) {
  return Promise.all(res.frames.map(b => new Promise(r => { const im = new Image(); im.onload = () => r(im); im.onerror = () => r(im); im.src = "data:image/jpeg;base64," + b; }))).then(a => {
    imgs = a;
    stackedCv = computeStack();
  });
}
function preparePlayer(res) {
  P = res; tpos = 0; playing = true; P.curDates = res.dates; P.curYears = res.years; P.curBase = true;
  const cv = $("spxCv"), f = $("pSpx").getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d);
  // above and below the square field: the same SPHEREx all-sky map the flight landed on
  backdrop = document.createElement("canvas"); backdrop.width = cv.width; backdrop.height = cv.height;
  const bg = backdrop.getContext("2d"); bg.fillStyle = "#13161a"; bg.fillRect(0, 0, cv.width, cv.height);
  if (skyCanvas) {
    const sw = skyCanvas.width * SF() / sky.W * (res.field_deg / req.fieldDeg), sh = sw * cv.height / cv.width;
    bg.filter = "grayscale(1) blur(1.5px) brightness(.7)";
    bg.drawImage(skyCanvas, (skyCanvas.width - sw) / 2, (skyCanvas.height - sh) / 2, sw, sh, 0, 0, cv.width, cv.height);
    bg.filter = "none";
  }
  tmp = document.createElement("canvas"); tmp.width = cv.width; tmp.height = cv.width;
  drawSpx();
}
function startPlayer() { last = performance.now(); cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); }
function stopPlayer() { cancelAnimationFrame(raf); P = null; }
let FPS = 5;
document.querySelectorAll("#pSpx .speed button").forEach(b => b.onclick = () => {
  FPS = 5 * +b.dataset.s; document.querySelectorAll("#pSpx .speed button").forEach(x => x.classList.toggle("on", x === b));
});
function loop(t) {
  if (!P) return;
  const dt = Math.max(0, Math.min(0.1, (t - last) / 1000)); last = t;
  if (playing && !dragging) tpos = (tpos + dt * FPS + imgs.length) % imgs.length;
  drawSpx(); raf = requestAnimationFrame(loop);
}
function drawSpx() {
  const cv = $("spxCv"), g = cv.getContext("2d"), n = imgs.length; if (!n || !P) return;
  const i = Math.floor(tpos) % n, fr = tpos - Math.floor(tpos), j = (i + 1) % n, e = smooth(fr);
  const cw = cv.width, ch = cv.height, S = ch, ox = (cw - S) / 2;
  g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
  if (backdrop) g.drawImage(backdrop, 0, 0);          // either side of the square field: the sky map, dimmed

  if (procMode === "stack" && stackedCv) {
    g.filter = `blur(${(S / P.nx * 0.08).toFixed(1)}px)`;
    g.drawImage(stackedCv, ox, 0, S, S);
    g.filter = "none";
  } else {
    g.filter = `blur(${(S / P.nx * 0.08).toFixed(1)}px)`;     // just enough to hide JPEG edges; the 6" pixels show honestly
    g.globalAlpha = 1; g.drawImage(imgs[i], ox, 0, S, S);
    if (e > 0.003) { g.globalAlpha = e; g.drawImage(imgs[j], ox, 0, S, S); g.globalAlpha = 1; }
    g.filter = "none";
    if (procMode === "diff" && wiseCanvas) {
      g.save();
      g.globalCompositeOperation = "difference";
      g.globalAlpha = 0.85;
      g.drawImage(wiseCanvas, ox, 0, S, S);
      g.restore();
    }
  }
  const Y = P.curYears || P.years, yr = Y ? (j > i ? Y[i] + (Y[j] - Y[i]) * e : Y[i]) : args.mid;
  for (const m of P.movers || []) {           // in every wavelength band: grey = archive position, dotted line, red = now
    const p0 = toPx(P, m.ra0, m.dec0, cw, ch), q = moverAt(m, yr), p1 = toPx(P, q[0], q[1], cw, ch);
    ghost(g, p0[0], p0[1], p1[0], p1[1], S * 0.78); ring(g, p1[0], p1[1], S * 0.64, 0.95);
  }

  const k = Math.round(tpos) % n, ds = P.curDates || P.dates;
  const tag = procMode === "diff" ? " · Difference" : procMode === "stack" ? " · Temporal Stack" : "";
  $("spxCap").textContent = (ds[k % ds.length] || "") + tag;
}

function initProcControls() {
  const mSub = $("modeSub"), mStk = $("modeStack"), sBtn = $("shareBtn");
  if (mSub) {
    mSub.onclick = () => {
      procMode = procMode === "diff" ? "normal" : "diff";
      mSub.classList.toggle("on", procMode === "diff");
      if (mStk) mStk.classList.remove("on");
      drawSpx();
    };
  }
  if (mStk) {
    mStk.onclick = () => {
      procMode = procMode === "stack" ? "normal" : "stack";
      mStk.classList.toggle("on", procMode === "stack");
      if (mSub) mSub.classList.remove("on");
      drawSpx();
    };
  }
  if (sBtn) {
    sBtn.onclick = () => {
      if (!last_res) return;
      const url = new URL(window.location.href);
      url.searchParams.set("ra", Number(last_res.ra).toFixed(4));
      url.searchParams.set("dec", Number(last_res.dec).toFixed(4));
      url.searchParams.set("fov", Math.round(last_res.field_deg * 3600));
      navigator.clipboard?.writeText(url.toString()).then(() => {
        toast("Direct coordinate link copied to clipboard!");
      }).catch(() => {
        toast(`Link: ?ra=${last_res.ra.toFixed(4)}&dec=${last_res.dec.toFixed(4)}`);
      });
    };
  }
}
/* before / after: the archive panel sits beside SPHEREx by default; the button hides or shows it */
app.classList.add("split");
function syncCmp(res) {
  const on = app.classList.contains("split");
  const t = on ? "Hide the comparison image" : `Compare with ${res?.compare?.name || "2MASS"} (${res?.compare?.year || "~2000"})`;
  $("cmpBtn").setAttribute("aria-pressed", String(on)); $("cmpBtn").setAttribute("aria-label", t); $("cmpBtn").title = t;
}
$("cmpBtn").onclick = () => {
  const on = !app.classList.contains("split");
  app.classList.toggle("split", on); $("cmpBtn").setAttribute("aria-pressed", String(on));
  syncCmp(last_res);
  requestAnimationFrame(() => { if (P) { const keep = [tpos, P.curDates, P.curBase, P.curYears]; preparePlayer(P); [tpos, P.curDates, P.curBase, P.curYears] = keep; drawSpx(); } if (last_res) { paintWise(last_res); fillObjects(last_res); } drawHl(); });
};
window.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const demoEl = $("demo");
    if (demoEl && demoEl.classList.contains("on")) {
      demoEl.classList.remove("on");
      const demoV = $("demoV");
      if (demoV) demoV.pause();
      leaveLanding();
    } else if (app.classList.contains("searching")) closeSearch();
    else if (app.classList.contains("about")) closeAbout();
  }
  if (e.key === " " && view === "view" && document.activeElement === document.body) { e.preventDefault(); playing = !playing; }
});

/* ---------------------------------------------------------------- search */
const CORE_PRESETS = [
  // NASA Space Apps Challenge Targets
  { key: "barnard", name: "Barnard's Star", ra: 269.448503, dec: 4.739420, pmra: -801.551, pmdec: 10362.394, epoch: 2016.0, fov: 300, detector: "SPHEREx-D3", kind: "High proper-motion red dwarf (10.3″/yr)" },
  { key: "epsindi", name: "Epsilon Indi B", ra: 331.076453, dec: -56.793812, pmra: 3987.41, pmdec: -2505.35, epoch: 2016.0, fov: 360, detector: "SPHEREx-D3", kind: "Cold brown dwarf binary companion" },
  { key: "wise0855", name: "WISE 0855-0714", ra: 133.795129, dec: -7.245147, pmra: -8118.9, pmdec: 679.3, epoch: 2010.5, fov: 360, detector: "SPHEREx-D6", kind: "Coldest known brown dwarf (8.1″/yr)" },
  { key: "rwaur", name: "RW Aurigae", ra: 76.956081, dec: 30.401205, pmra: 2.64, pmdec: -27.40, epoch: 2016.0, fov: 300, detector: "SPHEREx-D3", kind: "Young variable star with dimming disk" },
  { key: "rdra", name: "R Draconis", ra: 248.167378, dec: 66.755007, pmra: -20.64, pmdec: 6.15, epoch: 2016.0, fov: 300, detector: "SPHEREx-D3", kind: "Mira pulsating variable red giant" },
  // Fast-Moving Stars
  { key: "custom", name: "Kapteyn's Star", ra: 77.919, dec: -45.018, pmra: 6505.0, pmdec: -5731.0, epoch: 2000, fov: 300, detector: "SPHEREx-D3", kind: "Fast-moving halo subdwarf (8.7″/yr)" },
  { key: "custom", name: "61 Cygni", ra: 316.725, dec: 38.749, pmra: 4164.0, pmdec: 3250.0, epoch: 2000, fov: 300, detector: "SPHEREx-D3", kind: "Fast-moving binary star system (5.3″/yr)" },
  { key: "custom", name: "Groombridge 1830", ra: 178.245, dec: 37.719, pmra: 4003.0, pmdec: -5813.0, epoch: 2000, fov: 300, detector: "SPHEREx-D3", kind: "Fast-moving halo star (7.1″/yr)" },
  { key: "custom", name: "Wolf 359", ra: 164.120, dec: 7.015, pmra: -3842, pmdec: -2725, epoch: 2000, kind: "Nearby high-PM red dwarf (4.7″/yr)", fov: 300 },
  // Notable Sky Targets
  { key: "custom", name: "Sirius", ra: 101.287155, dec: -16.716116, pmra: -546.01, pmdec: -1223.07, epoch: 2000, kind: "Brightest star in night sky", fov: 400 },
  { key: "custom", name: "Alpha Centauri", ra: 219.902066, dec: -60.833975, pmra: -3679.25, pmdec: 473.67, epoch: 2000, kind: "Nearest star system to the Sun", fov: 450 },
  { key: "custom", name: "Proxima Centauri", ra: 217.428953, dec: -62.679484, pmra: -3781.74, pmdec: 769.47, epoch: 2000, kind: "Nearest individual star (3.85″/yr)", fov: 350 },
  { key: "custom", name: "Betelgeuse", ra: 88.792939, dec: 7.407064, pmra: 27.54, pmdec: 11.30, epoch: 2000, kind: "Red supergiant star in Orion", fov: 400 },
  { key: "custom", name: "Vega", ra: 279.234735, dec: 38.783689, pmra: 200.94, pmdec: 286.23, epoch: 2000, kind: "Standard photometric calibrator star", fov: 350 },
  { key: "custom", name: "Pleiades", ra: 56.75, dec: 24.116667, pmra: 0, pmdec: 0, epoch: 2000, kind: "Seven Sisters open star cluster (M45)", fov: 1200 },
  { key: "custom", name: "Polaris", ra: 37.954561, dec: 89.264109, pmra: 44.48, pmdec: -11.85, epoch: 2000, kind: "North celestial pole star", fov: 400 },
  { key: "custom", name: "Andromeda Galaxy", ra: 10.684708, dec: 41.268750, pmra: 0, pmdec: 0, epoch: 2000, kind: "Nearest major spiral galaxy (M31)", fov: 1800, size: 190 }
];

const sEl = $("search"), qEl = $("q"), listEl = $("results"), sbtn = $("sbtn");
let items = [], sel = -1, qid = 0;
const thumbCache = new Map(), thumbQ = [];
let thumbBusy = false;
function openSearch() {
  leaveLanding();
  if (view !== "home" && view !== "view") return;
  loadCatalog(); closeAbout(); sEl.classList.add("open"); app.classList.add("searching"); sbtn.setAttribute("aria-expanded", "true");
  tipEl.classList.remove("on"); showItems(presetItems()); setTimeout(() => qEl.focus(), 120);
}
function closeSearch(quick) {
  sEl.classList.remove("open"); app.classList.remove("searching"); sbtn.setAttribute("aria-expanded", "false"); qEl.blur();
  if (!quick) setTimeout(() => { if (!sEl.classList.contains("open")) qEl.value = ""; }, 400);
}
sbtn.onclick = () => (sEl.classList.contains("open") ? closeSearch() : openSearch());
$("veil").onclick = () => closeSearch();
const presetItems = () => {
  if (args.presets && args.presets.length) {
    const combined = [...args.presets.map(p => Object.assign({}, p, { kind: KINDS[p.key] || p.kind || "" }))];
    const seen = new Set(combined.map(c => c.name.toLowerCase()));
    for (const p of CORE_PRESETS) {
      if (!seen.has(p.name.toLowerCase())) { combined.push(p); seen.add(p.name.toLowerCase()); }
    }
    return combined;
  }
  return CORE_PRESETS;
};

function showItems(list) {
  items = list || []; sel = -1; listEl.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "no-results";
    li.textContent = "No celestial objects found. Type an object name or coordinates (e.g. 269.45, 4.74)";
    listEl.appendChild(li);
    return;
  }
  items.forEach((it, i) => {
    const li = document.createElement("li"); li.setAttribute("role", "option");
    li.innerHTML = `<div class="th"></div><div><div class="nm"></div><div class="kd"></div></div>`;
    li.querySelector(".nm").textContent = it.name; li.querySelector(".kd").textContent = it.kind || "";
    li.onclick = () => choose(it); li.onmouseenter = () => mark(i);
    listEl.appendChild(li); thumb(it, li.querySelector(".th"));
  });
}
function mark(i) { sel = i; [...listEl.children].forEach((c, j) => c.classList.toggle("on", j === i)); }
function choose(it) {
  const fov = it.key !== "custom" ? it.fov : Math.min(7200, Math.max(300, 1.3 * 60 * (it.size || 0)));
  go(Object.assign({ epoch: 2000, pmra: 0, pmdec: 0, detector: "SPHEREx-D3" }, it, { fov: fov || 300 }));
}

function thumb(it, box) {
  const key = it.key + "|" + it.name;
  if (thumbCache.has(key)) { box.appendChild(thumbCache.get(key).cloneNode(true)); box.firstChild.classList.add("ok"); return; }
  thumbQ.push({ it, box, key }); pump();
}
async function pump() {
  if (thumbBusy) return; thumbBusy = true;
  while (thumbQ.length) {
    const { it, box, key } = thumbQ.shift(); if (!box.isConnected) continue;
    try {
      const p = propagate(it, 1995.5);                // optical plates are from the 1990s
      const c = await renderCutout(SURVEYS.dss, p[0], p[1], 0.05, 96, 96, { gain: 1.5, gamma: 0.85, black: 0.04 });
      thumbCache.set(key, c); if (box.isConnected) { const k = c.cloneNode(); k.getContext("2d").drawImage(c, 0, 0); box.appendChild(k); requestAnimationFrame(() => k.classList.add("ok")); }
    } catch (e) { /* no picture: the row still works */ }
  }
  thumbBusy = false;
}

let deb = 0;
qEl.addEventListener("input", () => {
  clearTimeout(deb); const q = qEl.value.trim(), my = ++qid;
  if (!q) return showItems(presetItems());
  const qLower = q.toLowerCase();
  const local = presetItems().filter(p => p.name.toLowerCase().includes(qLower) || (p.kind && p.kind.toLowerCase().includes(qLower))).concat(famous(q));
  const num = parseCoords(q); const first = num ? [num] : local;
  showItems(first);
  if (!num && q.length >= 2) deb = setTimeout(() => remote(q, my, first), 260);
});
qEl.addEventListener("keydown", e => {
  if (e.key === "ArrowDown") { e.preventDefault(); mark(Math.min(items.length - 1, sel + 1)); }
  else if (e.key === "ArrowUp") { e.preventDefault(); mark(Math.max(0, sel - 1)); }
  else if (e.key === "Enter" && items.length) { choose(items[Math.max(0, sel)]); }
});
function parseCoords(q) {
  let s = q.trim();
  // Check for sexagesimal like 19h 43m 40.6s -46d 39m 15s or RA 19h 43m 40.6s Dec -46° 39' 15"
  const sexaRegex = /^(?:RA\s*:?\s*)?(\d+)[h\s:]+(\d+)[m\s:]+([\d.]+)[s\s,]+(?:Dec\s*:?\s*)?([+-]?\d+)[d\s:°]+(\d+)[m\s:\x27]+([\d.]+)s?["\x27]?$/i;
  const match = s.match(sexaRegex);
  if (match) {
    const h = +match[1], m = +match[2], sec = +match[3];
    const dSign = match[4].startsWith("-") ? -1 : 1;
    const d = Math.abs(+match[4]), dm = +match[5], dsec = +match[6];
    const ra = ((h + m / 60 + sec / 3600) * 15 + 360) % 360;
    const dec = dSign * (d + dm / 60 + dsec / 3600);
    return {
      key: "custom",
      name: `${ra.toFixed(3)}°, ${dec >= 0 ? "+" : ""}${dec.toFixed(3)}°`,
      ra,
      dec,
      kind: "Coordinates (Sexagesimal)",
      epoch: args.mid,
    };
  }
  // Check for decimal with or without RA/Dec prefix, e.g. "RA 295.919, Dec -46.654" or "295.919, -46.654"
  s = s.replace(/RA\s*:?\s*/gi, "").replace(/Dec\s*:?\s*/gi, "");
  const parts = s.replace(/[,;]/g, " ").trim().split(/\s+/).map(Number);
  if (parts.length === 2 && parts.every(isFinite) && Math.abs(parts[1]) <= 90) {
    return {
      key: "custom",
      name: `${parts[0].toFixed(3)}°, ${parts[1] >= 0 ? "+" : ""}${parts[1].toFixed(3)}°`,
      ra: ((parts[0] % 360) + 360) % 360,
      dec: parts[1],
      kind: "Coordinates",
      epoch: args.mid,
    };
  }
  return null;
}
const esc = s => s.replace(/'/g, "''");
const FAMOUS = [
  ["Sirius", 101.287155, -16.716116, -546.01, -1223.07, "Brightest star"], ["Polaris", 37.954561, 89.264109, 44.48, -11.85, "North Star"],
  ["Alpha Centauri", 219.902066, -60.833975, -3679.25, 473.67, "Nearest star system"], ["Proxima Centauri", 217.428953, -62.679484, -3781.74, 769.47, "Nearest star"],
  ["TON 618", 187.104025, 31.477119, 0, 0, "Quasar"], ["Betelgeuse", 88.792939, 7.407064, 27.54, 11.30, "Red supergiant"],
  ["Vega", 279.234735, 38.783689, 200.94, 286.23, "Star"], ["Rigel", 78.634467, -8.201638, 1.31, 0.50, "Blue supergiant"],
  ["Arcturus", 213.915300, 19.182409, -1093.39, -2000.06, "Red giant"], ["Andromeda Galaxy", 10.684708, 41.268750, 0, 0, "Galaxy", 190],
  ["Orion Nebula", 83.822083, -5.391111, 0, 0, "Nebula", 66], ["Sagittarius A*", 266.416837, -29.007810, 0, 0, "Galactic centre"],
  ["Eta Carinae", 161.264775, -59.684431, -7.6, 1.0, "Massive star"], ["Crab Nebula", 83.633083, 22.014500, 0, 0, "Supernova remnant", 7],
  ["3C 273", 187.277915, 2.052388, 0, 0, "Quasar"], ["Pleiades", 56.75, 24.116667, 0, 0, "Star cluster", 110],
  ["Kapteyn's Star", 77.919, -45.018, 6505, -5731, "Fast-moving star"], ["61 Cygni", 316.725, 38.749, 4164, 3250, "Fast-moving pair"],
  ["Groombridge 1830", 178.245, 37.719, 4003, -5813, "Fast-moving star"], ["Wolf 359", 164.120, 7.015, -3842, -2725, "Nearby red dwarf"],
].map(([name, ra, dec, pmra, pmdec, kind, size]) => ({ key: "custom", name, ra, dec, pmra, pmdec, epoch: 2000, kind, size: size || 0 }));
// the local directory: every SIMBAD object with a common name, all Messier / NGC / IC objects and ~5000 well-studied stars
let CATALOG = null, catLoading = null;
function loadCatalog() {
  if (!catLoading) catLoading = fetch("catalog.json").then(r => r.json()).then(rows => {
    CATALOG = rows.map(([name, ra, dec, pmra, pmdec, otype, size, nb]) => ({ key: "custom", name, ra, dec, pmra, pmdec, epoch: 2000,
      kind: OTYPE[otype] || otype, size, nb, lc: name.toLowerCase() }));
    if (qEl.value.trim()) qEl.dispatchEvent(new Event("input"));   // refresh results typed before the directory arrived
    return CATALOG;
  }).catch(() => (CATALOG = []));
  return catLoading;
}
function famous(q) {
  const s = q.toLowerCase().replace(/\s+/g, " ").trim(), sq = s.replace(/\s/g, "");
  const out = FAMOUS.filter(f => f.name.toLowerCase().includes(s));
  if (CATALOG) {
    const hit = [];
    for (const c of CATALOG) {                          // starts with the query, or a word in the name does ("crab" -> "Crab Nebula")
      if (c.lc.startsWith(s) || c.lc.replace(/\s/g, "").startsWith(sq) || c.lc.includes(" " + s)) { hit.push(c); if (hit.length > 40) break; }
    }
    hit.sort((a, b) => ((b.lc === s || b.lc.replace(/\s/g, "") === sq) - (a.lc === s || a.lc.replace(/\s/g, "") === sq)) || (b.lc.startsWith(s) - a.lc.startsWith(s)) || (b.nb - a.nb));
    const seenPos = new Set(out.map(o => o.ra.toFixed(2) + o.dec.toFixed(2)));
    for (const h of hit) { const k = h.ra.toFixed(2) + h.dec.toFixed(2); if (!seenPos.has(k)) { seenPos.add(k); out.push(h); } }
  }
  return out.slice(0, 8);
}
const timed = (url, ms) => { const c = new AbortController(); const t = setTimeout(() => c.abort(), ms); return fetch(url, { signal: c.signal }).finally(() => clearTimeout(t)); };
function parseSesame(t, q) {
  const J = t.match(/%J\s+([\d.]+)\s+([+-][\d.]+)/), P2 = t.match(/%P\s+([-\d.]+)\s+([-\d.]+)/), N = t.match(/%I\.0\s+(.+)/), C = t.match(/%C\.0\s+(\S+)/);
  return J ? [{ key: "custom", name: (N ? N[1] : q).trim().replace(/^NAME\s+/, ""), ra: +J[1], dec: +J[2], pmra: P2 ? +P2[1] : 0, pmdec: P2 ? +P2[2] : 0, epoch: 2000, kind: C ? (OTYPE[C[1]] || C[1]) : "" }] : [];
}
async function remote(q, my, have) {
  const cap = q[0].toUpperCase() + q.slice(1), vars = [...new Set([q, cap, q.toUpperCase()])];
  const like = vars.flatMap(v => [`'${esc(v)}%'`, `'NAME ${esc(v)}%'`, `'* ${esc(v)}%'`, `'V* ${esc(v)}%'`]).map(p => `id LIKE ${p}`).join(" OR ");
  const adql = `SELECT TOP 8 main_id, ra, dec, pmra, pmdec, otype, galdim_majaxis FROM basic JOIN ident ON oid = oidref WHERE ${like}`;
  const simbad = timed("https://simbad.harvard.edu/simbad/sim-tap/sync?request=doQuery&lang=adql&format=json&query=" + encodeURIComponent(adql), 7000)
    .then(r => r.json()).then(j => (j.data || []).filter(x => x[1] != null).map(x => ({ key: "custom", name: String(x[0]).replace(/^NAME\s+/, "").replace(/^\*\s+/, ""), ra: x[1], dec: x[2], pmra: x[3] || 0, pmdec: x[4] || 0, epoch: 2000, kind: OTYPE[x[5]] || x[5] || "", size: x[6] || 0 })))
    .catch(() => []);
  const mirrors = ["https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/SNV?", "https://vizier.cfa.harvard.edu/viz-bin/nph-sesame/-oI/SNV?", "https://vizier.idia.ac.za/viz-bin/nph-sesame/-oI/SNV?"];
  const sesame = Promise.any(mirrors.map(m => timed(m + encodeURIComponent(q), 6000).then(r => r.text()).then(t => { const o = parseSesame(t, q); if (!o.length) throw 0; return o; }))).catch(() => []);
  const tidy = n => n.replace(/^(NAME|V\*|\*\*|\*)\s+/, "").replace(/\s+/g, " ").trim();
  const spot = o => (+o.ra).toFixed(2) + "," + (+o.dec).toFixed(2);
  const show = out => {
    if (my !== qid || !sEl.classList.contains("open") || !out.length) return;
    out.forEach(o => { o.name = tidy(o.name); });
    const seen = new Set(have.map(h => h.name.toLowerCase())), seenAt = new Set(have.map(spot));
    const add = out.filter(o => { const k = spot(o); if (seen.has(o.name.toLowerCase()) || seenAt.has(k)) return false; seenAt.add(k); return true; });
    have = have.concat(add).slice(0, 8); showItems(have);
  };
  sesame.then(show); simbad.then(show);                  // whichever answers first shows first
}

/* ---------------------------------------------------------------- top bar */
let page = null;
function openAbout(which = "how") {
  leaveLanding();
  if (busy && view !== "view") return; closeSearch(true); page = which; app.classList.add("about");
  document.querySelectorAll("#about .pg").forEach(d => d.classList.toggle("on", d.dataset.page === which));
  document.querySelectorAll("#bar .navb").forEach(b => b.classList.toggle("on", b.dataset.page === which));
}
function closeAbout() { page = null; app.classList.remove("about"); document.querySelectorAll("#bar .navb").forEach(b => b.classList.remove("on")); }
document.querySelectorAll("#bar .navb").forEach(b => b.onclick = () => (page === b.dataset.page ? closeAbout() : openAbout(b.dataset.page)));
$("brand").onclick = () => { closeSearch(true); if (app.classList.contains("about")) closeAbout(); if (view === "view" || view === "waiting") leave(); };

initProcControls();

/* ---------------------------------------------------------------- landing: Demo / Explore */
app.classList.add("landing");
intro.preload().catch(() => {});
const leaveLanding = () => app.classList.remove("landing");
function fillStarts() {
  const sl = $("startList"); if (!sl || sl.childElementCount) return;
  const items = presetItems(); if (!items.length) return;
  items.slice(0, 4).forEach(p => { const b = document.createElement("button"); b.type = "button";
    b.textContent = p.name; b.onclick = () => { leaveLanding(); choose(p); }; sl.appendChild(b); });
  sl.parentNode.classList.add("ready");
}
fillStarts(); setInterval(fillStarts, 500);

const demoEl = $("demo"), demoV = $("demoV"), demoSkip = $("demoSkip");
function closeDemo() {
  if (demoEl) demoEl.classList.remove("on");
  if (demoV) demoV.pause();
  leaveLanding();
}

$("exploreBtn").onclick = () => {
  leaveLanding();
  openSearch();
};

$("demoBtn").onclick = () => {
  if (demoEl && demoV) {
    demoEl.classList.add("on");
    demoV.currentTime = 0;
    demoV.play().catch(() => {});
  } else {
    leaveLanding();
  }
};

if (demoSkip) demoSkip.onclick = closeDemo;
if (demoV) demoV.onended = closeDemo;

/* ---------------------------------------------------------------- past & future: the 3D depth view */
let depthOpen = false;
function enterDepth() {
  if (!last_res) return;
  app.classList.add("deep"); $("timeBtn").textContent = "Back to images"; $("timeBtn").setAttribute("aria-pressed", "true");
  $("tslider").value = 0; $("tLab").innerHTML = depth.timeLabel(0); buildTicks();
  if (last_res.depth) showDepth(last_res);
  else { $("depthWait").classList.add("on"); emit({ type: "depth", nonce: "d" + Date.now(), for: last_res.nonce }); }
}
function exitDepth() {
  app.classList.remove("deep"); depth.close(); depthOpen = false;
  $("timeBtn").textContent = "3D map: distance & time"; $("timeBtn").setAttribute("aria-pressed", "false");
  requestAnimationFrame(() => { if (P) { const keep = [tpos, P.curDates, P.curBase, P.curYears]; preparePlayer(P); [tpos, P.curDates, P.curBase, P.curYears] = keep; drawSpx(); } if (last_res) paintWise(last_res); placeKnob(); });
}
async function showDepth(res) {
  last_res.depth = res.depth;
  const d = res.depth, n = (d.objects || []).length;
  $("depthCap").textContent = d.error ? d.error : n ? `${n.toLocaleString()} objects` : "No measured distances here yet";
  $("depthCap").title = n ? `Everything with a measured distance within ${Math.round(d.radius_deg * 60)}′ of the centre` : "";
  $("depthWait").classList.remove("on");
  if (!app.classList.contains("deep")) return;
  await depth.open($("pDepth"), d, last_res); depthOpen = true;
}
function buildTicks() {                        // little marks under the time slider, with Today in the middle
  const box = $("tticks"); if (box.childElementCount) return;
  for (const [y, lab] of depth.TICKS) {
    const pct = (depth.sliderPos(y) + 1000) / 20, now = y === 0, i = document.createElement("i"), em = document.createElement("em");
    i.style.left = pct + "%"; em.style.left = pct + "%"; em.textContent = lab;
    if (now) { i.className = "now"; em.className = "now"; } else if (pct < 2) em.className = "l"; else if (pct > 98) em.className = "r";
    box.append(i, em);
  }
}
$("timeBtn").onclick = () => (app.classList.contains("deep") ? exitDepth() : enterDepth());
$("tslider").addEventListener("input", e => { const t = depth.sliderYears(+e.target.value); depth.setTime(t); $("tLab").innerHTML = depth.timeLabel(t, last_res && last_res.depth, last_res); });

function onArgs() {
  if (args.result) leaveLanding();
  if (view === "view" && args.result && last_res && args.result.nonce === last_res.nonce && args.result.depth && !depthOpen && app.classList.contains("deep")) showDepth(args.result);
  if (view === "view" && args.result && last_res && args.result.nonce === last_res.nonce && args.result.bands && !last_res.bands) onBands(args.result);
  tryReveal();
}   // a deep link or a result already loaded: no landing screen

(async () => {
  try {
    const init = await fetch("/api/stage/init").then(r => r.json());
    if (init.presets) args.presets = init.presets;
    if (init.mid) args.mid = init.mid;
  } catch (e) {}
  await boot();
  const qp = new URLSearchParams(window.location.search);
  if (qp.has("ra") && qp.has("dec")) {
    const ra = parseFloat(qp.get("ra")), dec = parseFloat(qp.get("dec"));
    const fov = parseFloat(qp.get("fov") || "180");
    if (Number.isFinite(ra) && Number.isFinite(dec)) {
      leaveLanding();
      go({ key: "custom", name: "Linked location", ra, dec, pmra: 0, pmdec: 0, epoch: args.mid, fov, detector: "SPHEREx-D3" });
    }
  }
})();
}
