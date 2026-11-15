import { Sky, unit, radec, slerp } from "./sky.js";
import { renderCutout, SURVEYS } from "./healpix.js";

/* ---------------------------------------------------------------- Streamlit bridge */
const send = (type, extra) => window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type }, extra), "*");
const emit = value => send("streamlit:setComponentValue", { value, dataType: "json" });
let args = { presets: [], mid: 2026.3, result: null, error: null };
window.addEventListener("message", e => {
  if (e.data && e.data.type === "streamlit:render") { args = Object.assign(args, e.data.args); onArgs(); }
});
send("streamlit:componentReady", { apiVersion: 1 });

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
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.id); toast.id = setTimeout(() => t.classList.remove("on"), 4200); }

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
  if (view !== "home" || sky.fov() > 2) return;
  const [r, d] = sky.center(), w = Math.round(sky.W / 1.5), h = Math.round(sky.H / 1.5);
  const black = (sky.black ?? 18) / 255;
  try {
    const c = await renderCutout(SURVEYS.tmass, r, d, sky.fov(), w, h, {});
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
  window.addEventListener("pointerup", () => { if (view === "home" && sky.fov() < 8) setTimeout(refreshSmooth, 200); });
  const homeOk = () => view === "home" && !busy && !app.classList.contains("searching") && !app.classList.contains("about");
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
 clearTimeout(wt); smoothCv.classList.remove("on"); smoothId++; wt = setTimeout(async () => { await autoCuts(); refreshSmooth();  }, 300); }, { capture: true, passive: false });
  clip.addEventListener("contextmenu", e => { e.preventDefault(); e.stopPropagation(); }, true);
  window.addEventListener("contextmenu", e => { if (clip.contains(e.target)) { e.preventDefault(); e.stopPropagation(); } }, true);
  // right button: draw an area. Swallowed before Aladin sees it (Aladin uses right-drag for contrast).
  let sel = null;
  const stop = e => { e.stopPropagation(); e.preventDefault(); };
  const begin = e => {
    if (e.button !== 2) { if (homeOk() && e.button === 0) wake(); return; }
    stop(e); if (!homeOk() || sel) return;
    wake(); const r = rect();
    sel = { x0: e.clientX - r.left, y0: e.clientY - r.top, x1: e.clientX - r.left, y1: e.clientY - r.top };
    drawSel(); selEl.classList.add("on"); tipDone = true; tipEl.classList.remove("on");
  };
  clip.addEventListener("pointerdown", begin, true);
  clip.addEventListener("mousedown", e => { if (e.button === 2) stop(e); }, true);
  const drawSel = () => {
    const l = Math.min(sel.x0, sel.x1), t = Math.min(sel.y0, sel.y1);
    Object.assign(selEl.style, { left: l + "px", top: t + "px", width: Math.abs(sel.x1 - sel.x0) + "px", height: Math.abs(sel.y1 - sel.y0) + "px" });
  };
  window.addEventListener("pointermove", e => { if (!sel) return; stop(e); const r = rect(); sel.x1 = e.clientX - r.left; sel.y1 = e.clientY - r.top; drawSel(); }, true);
  window.addEventListener("mousemove", e => { if (sel) stop(e); }, true);
  window.addEventListener("mouseup", e => { if (sel && e.button === 2) stop(e); }, true);
  window.addEventListener("pointerup", e => {
    if (!sel || e.button !== 2) return; stop(e);
    const s0 = sel; sel = null;
    const l = Math.min(s0.x0, s0.x1), r_ = Math.max(s0.x0, s0.x1), t = Math.min(s0.y0, s0.y1), b = Math.max(s0.y0, s0.y1);
    const cx = (l + r_) / 2, cy = (t + b) / 2;
    if (r_ - l < 4 || b - t < 4) { selEl.classList.remove("on"); return; }
    const c = sky.pix2world(cx, cy), L = sky.pix2world(l, cy), R = sky.pix2world(r_, cy), T = sky.pix2world(cx, t), B = sky.pix2world(cx, b);
    if (!c || !L || !R || !T || !B) { selEl.classList.remove("on"); return toast("Keep the area inside the map."); }
    // the field is the square that covers the whole highlighted area, centred on it
    const side = Math.max(sepDeg(L, R), sepDeg(T, B)) * 3600;
    if (side > 5400) { selEl.classList.remove("on"); return toast(`That area is ${(side / 3600).toFixed(side > 36000 ? 0 : 1)}° wide. Scroll to zoom in and pick one under 1.5°.`); }
    go({ key: "custom", name: "Selected area", ra: c[0], dec: c[1], pmra: 0, pmdec: 0, epoch: args.mid, fov: Math.max(60, side) });
  }, true);
}

/* ---------------------------------------------------------------- flight */
const panelA = () => { const r = $("pSpx").getBoundingClientRect(); return r.width ? r.height / r.width : 1.4; };
// width of the field on screen at the end of the flight; the panel window (width x width*a) must fit inside the viewport
const ENDFOV = () => req.fieldDeg * sky.W / (SF() * panelA());   // sky width so the field spans the panel's height
const SF = () => Math.min(sky.W * 0.4, 0.8 * (sky.H - 20) / panelA());
let nonce = 0;

// Fly to a target and never lose it: (1) turn the globe until the target sits a little below-right of the
// centre, zooming only as much as keeps it on screen; (2) zoom in while it glides diagonally to the centre,
// landing exactly on the final view (no overshoot).
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
async function flyTo(ra, dec, fov, dur) {
  const T = [ra, dec], c0 = sky.center(), f0 = sky.fov(), aspect = sky.H / sky.W;
  const start = offsetOf(c0, T), startFrac = [start[0] / f0, start[1] / f0];
  const visible = Math.abs(startFrac[0]) < 0.42 && Math.abs(startFrac[1]) < 0.42 * aspect && f0 < 200;
  const fA = visible ? f0 : Math.max(fov, Math.min(f0 * 0.55, 90));
  const kA = visible ? startFrac : OFF;
  const tA = visible ? 0 : 0.38;                     // share of the time spent turning the globe
  const u0 = unit(c0[0], c0[1]), cA = centreFor(T, [kA[0] * fA, kA[1] * fA]), uA = unit(cA[0], cA[1]);
  await tween(dur, t => {
    if (t < tA) {                                    // phase 1: turn, zoom a little, target always inside the view
      const p = smooth(t / tA), c = radec(slerp(u0, uA, p));
      const d = Math.hypot(...offsetOf(c, T));
      const f = Math.max(Math.exp(Math.log(f0) + (Math.log(fA) - Math.log(f0)) * p), d * 3.4);
      sky.set(c[0], c[1], Math.min(f, sky.homeFov()));        // zooms out as far as needed to keep the target in view
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
async function flyHome(ra, dec, fov, dur) {         // back out to the whole sky
  const c0 = sky.center(), u0 = unit(c0[0], c0[1]), u1 = unit(ra, dec), f0 = Math.log(sky.fov()), f1 = Math.log(fov);
  await tween(dur, t => { const p = smooth(t), [r, d] = radec(slerp(u0, u1, p)); sky.set(r, d, Math.exp(f0 + (f1 - f0) * p)); });
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
  closeSearch(true); closeAbout();
  if (view === "view") {                           // from the three panels: fold back to the sky, then fly on to the next target
    busy = true; stopPlayer(); setView("leaving"); app.classList.remove("shown");
    await unsquish();
    if (overlay) { const o = overlay; overlay = null; o.style.transition = "opacity .5s"; o.style.opacity = "0"; setTimeout(() => o.remove(), 600); }
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
  // 1) the SPHEREx all-sky map of the whole window, smoothed, to take over from Aladin's pixels as the zoom lands
  const cw = 768, ch = Math.round(cw * sky.H / sky.W);
  renderCutout(SURVEYS.tmass8, req.pos[0], req.pos[1], ENDFOV(), cw, ch, {}).then(c => {
    if (!req || req.nonce !== my) return;
    skyCanvas = c; overlay.width = c.width; overlay.height = c.height; overlay.getContext("2d").drawImage(c, 0, 0);
    app.classList.add("smoothready"); if (req.arrived) overlay.style.opacity = "1";
  }).catch(() => {});
  // 2) the archive image at the same wavelength, same window, for the left panel
  const f = $("pSpx").getBoundingClientRect(), pw = Math.min(560, Math.round(f.width * Math.min(2, devicePixelRatio || 1))), ph = Math.round(pw * f.height / f.width);
  req.wiseJob = { survey: cmpI.survey, pw, ph };
  // the server builds the SPHEREx field with exactly this centre and size, so the AllWISE picture can start now
  req.wiseJob.p = renderCutout(req.wiseJob.survey, req.pos[0], req.pos[1], req.fieldDeg * pw / ph, pw, ph, WISETONE).catch(() => null);
  await flyTo(req.pos[0], req.pos[1], ENDFOV(), 6500);
  req.arrived = true; autoCuts(); if (skyCanvas) overlay.style.opacity = "1";
  setView("waiting"); tryReveal();
}

/* ---------------------------------------------------------------- reveal (squish into the SPHEREx panel) */
function layoutField() {}
const geom = () => {
  const W = sky.W, H = sky.H, f = $("pSpx").getBoundingClientRect(), S = SF();
  return { W, H, f, S, k: f.width / S, A: f.height / f.width, dx: f.left + f.width / 2 - W / 2, dy: f.top + f.height / 2 - H / 2 };
};
async function tryReveal() {
  const res = args.result;
  if (!req || !req.arrived || view === "view" || view === "settling") return;
  if (args.error) { toast(args.error); return abortHome(); }
  if (!res || res.nonce !== req.nonce) return;
  setView("settling");
  fillPage(res);
  await loadFrames(res);
  try {                                                     // same centre, same size, same orientation as the SPHEREx cube
    const j = req.wiseJob, same = Math.abs(res.field_deg / req.fieldDeg - 1) < 0.02 &&
      Math.hypot(...offsetOf([res.ra_c, res.dec_c], req.pos)) * 3600 < 6;
    const job = same ? j.p : renderCutout(j.survey, res.ra_c, res.dec_c, res.field_deg * j.pw / j.ph, j.pw, j.ph, WISETONE);
    wiseCanvas = await Promise.race([job, new Promise(r => setTimeout(() => r(null), 20000))]);
  } catch (e) { wiseCanvas = null; }
  const { W, H, S, k, A, dx, dy } = geom();
  preparePlayer(res);                                       // frame 1 drawn underneath, invisible
  setTimeout(() => app.classList.add("shown"), 300);
  await tween(1100, t => {
    const p = smooth(t), w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
    inner.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
    clip.style.clipPath = `inset(${cy - h / 2}px ${W - cx - w / 2}px ${H - cy - h / 2}px ${cx - w / 2}px round ${26 * smooth(clamp01(p * 1.3))}px)`;
  });
  paintWise(res);
  $("spxCv").classList.add("on");                           // the map hands over to the real SPHEREx frames
  await new Promise(r => setTimeout(r, 500));
  clip.style.visibility = "hidden";
  setView("view"); startPlayer();
}

function abortHome() { busy = false; req = null; leave(true); }

async function leave(fromError = false) {
  if (busy && !fromError && view !== "view" && view !== "waiting") return;
  stopPlayer();
  const was = view;
  emit({ type: "home", nonce: "h" + Date.now() });
  setView("leaving");
  app.classList.remove("shown");
  selEl.classList.remove("on");
  if (was === "view" || was === "settling") await unsquish();
  clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
  if (overlay) { const o = overlay; overlay = null; o.style.transition = "opacity .5s"; o.style.opacity = "0"; setTimeout(() => o.remove(), 600); }
  setView("flying");
  const c = sky.center();
  await flyHome(c[0], 0, sky.homeFov(), 3200);
  setView("home"); fitBlend(); busy = false; req = null; sky.grid(false); autoCuts(); app.classList.remove("zoomed"); sky.startSpin(2.2); spinning = true;
}
// the three panels fold back into the full-screen sky (reverse of the reveal)
async function unsquish() {
  {
    const { W, H, S, k, A, dx, dy } = geom();
    clip.style.visibility = "visible"; $("spxCv").classList.remove("on");
    await new Promise(r => setTimeout(r, 300));
    await tween(900, t => {
      const p = 1 - smooth(t), w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
      inner.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
      const g = smooth(t);
      clip.style.clipPath = `inset(${(cy - h / 2) * (1 - g)}px ${(W - cx - w / 2) * (1 - g)}px ${(H - cy - h / 2) * (1 - g)}px ${(cx - w / 2) * (1 - g)}px round ${26 * (1 - t)}px)`;
    });
  }
  clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
}

/* ---------------------------------------------------------------- page two content */
const hasPM = res => Math.hypot(res.pmra || 0, res.pmdec || 0) > 50;     // a ring only helps for real movers
function fillPage(res) {
  $("iName").textContent = res.name;
  const [a, b] = sexa(res.ra, res.dec);
  $("iCoord").innerHTML = `${a}<br>${b}`;
  const v = res.verdict;
  $("iVerdict").innerHTML = `<div class="chip tone-${v.tone}"><i></i>${v.word}</div>` +
    `<div class="conf">Confidence <b>${v.conf}</b></div>` +
    `<div class="dots">${v.checks.map(c => `<span class="${c.ok ? "ok" : "no"}"><em></em>${c.k}</span>`).join("")}</div>`;
  $("wiseH").textContent = res.compare.name;
  $("wiseCap").textContent = res.compare.year || "2010";
}
const SPX_PSF = 9.0, WISE_PSF = { W1: 6.1, W2: 6.4, J: 2.9, H: 2.8, K: 2.9 };   // arcsec FWHM
function paintWise(res) {
  const cv = $("wiseCv"), f = $("pWise").getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d);
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
    let lo = v[Math.floor(v.length * 0.5)], hi = Math.max(lo + 20, v[Math.floor(v.length * 0.995)]);
    if (lo > 230) { lo = v[0]; hi = 255; }        // saturated archive tiles (Galactic centre): keep what detail there is
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
function loadFrames(res) {
  return Promise.all(res.frames.map(b => new Promise(r => { const im = new Image(); im.onload = () => r(im); im.onerror = () => r(im); im.src = "data:image/jpeg;base64," + b; }))).then(a => { imgs = a; });
}
function preparePlayer(res) {
  P = res; tpos = 0; playing = true; $("play").classList.remove("paused");
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
const FPS = 5;
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
  g.filter = `blur(${(S / P.nx * 0.25).toFixed(1)}px)`;
  g.globalAlpha = 1; g.drawImage(imgs[i], ox, 0, S, S);
  if (e > 0.003) { g.globalAlpha = e; g.drawImage(imgs[j], ox, 0, S, S); g.globalAlpha = 1; }
  g.filter = "none";
  const yr = P.years ? (j > i ? P.years[i] + (P.years[j] - P.years[i]) * e : P.years[i]) : args.mid;
  for (const m of P.movers || []) {                 // grey = 2010 (AllWISE), dotted line, red = now (this SPHEREx frame)
    const p0 = toPx(P, m.ra0, m.dec0, cw, ch), q = moverAt(m, yr), p1 = toPx(P, q[0], q[1], cw, ch);
    ghost(g, p0[0], p0[1], p1[0], p1[1], S * 0.78); ring(g, p1[0], p1[1], S * 0.64, 0.95);
  }

  const k = Math.round(tpos) % n;
  $("spxCap").textContent = P.dates[k];
  const frac = n > 1 ? tpos / (n - 1) : 0, tr = $("track").clientWidth;
  $("fill").style.width = Math.min(1, frac) * tr + "px"; $("thumb").style.left = Math.min(1, frac) * tr + "px";
}
$("play").onclick = () => { playing = !playing; $("play").classList.toggle("paused", !playing); };
(() => {
  const tr = $("track"); let was = true;
  const seek = e => { const r = tr.getBoundingClientRect(); tpos = clamp01((e.clientX - r.left) / r.width) * Math.max(1, imgs.length - 1); if (P) drawSpx(); };
  tr.addEventListener("pointerdown", e => { dragging = true; was = playing; tr.setPointerCapture(e.pointerId); seek(e); });
  tr.addEventListener("pointermove", e => { if (dragging) seek(e); });
  tr.addEventListener("pointerup", () => { dragging = false; });
})();
window.addEventListener("keydown", e => {
  if (e.key === "Escape") { if (app.classList.contains("searching")) closeSearch(); else if (app.classList.contains("about")) closeAbout(); }
  if (e.key === " " && view === "view" && document.activeElement === document.body) { e.preventDefault(); $("play").click(); }
});

/* ---------------------------------------------------------------- search */
const sEl = $("search"), qEl = $("q"), listEl = $("results"), sbtn = $("sbtn");
let items = [], sel = -1, qid = 0;
const thumbCache = new Map(), thumbQ = [];
let thumbBusy = false;
function openSearch() {
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
const presetItems = () => args.presets.map(p => Object.assign({}, p, { kind: KINDS[p.key] || "" }));

function showItems(list) {
  items = list; sel = -1; listEl.innerHTML = "";
  list.forEach((it, i) => {
    const li = document.createElement("li"); li.setAttribute("role", "option");
    li.innerHTML = `<div class="th"></div><div><div class="nm"></div><div class="kd"></div></div>`;
    li.querySelector(".nm").textContent = it.name; li.querySelector(".kd").textContent = it.kind || "";
    li.onclick = () => choose(it); li.onmouseenter = () => mark(i);
    listEl.appendChild(li); thumb(it, li.querySelector(".th"));
  });
}
function mark(i) { sel = i; [...listEl.children].forEach((c, j) => c.classList.toggle("on", j === i)); }
function choose(it) {
  const fov = it.key !== "custom" ? it.fov : Math.min(5400, Math.max(300, 1.3 * 60 * (it.size || 0)));
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
  const local = presetItems().filter(p => p.name.toLowerCase().includes(q.toLowerCase())).concat(famous(q));
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
  const m = q.replace(/[,;]/g, " ").trim().split(/\s+/).map(Number);
  if (m.length === 2 && m.every(isFinite) && Math.abs(m[1]) <= 90) return { key: "custom", name: `${m[0].toFixed(3)}°, ${m[1] >= 0 ? "+" : ""}${m[1].toFixed(3)}°`, ra: ((m[0] % 360) + 360) % 360, dec: m[1], kind: "Coordinates", epoch: args.mid };
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
function openAbout() { if (busy && view !== "view") return; closeSearch(true); app.classList.add("about"); }
function closeAbout() { app.classList.remove("about"); }
$("aboutBtn").onclick = () => (app.classList.contains("about") ? closeAbout() : openAbout());
$("brand").onclick = () => { closeSearch(true); if (app.classList.contains("about")) closeAbout(); if (view === "view" || view === "waiting") leave(); };

function onArgs() { tryReveal(); }
boot();
