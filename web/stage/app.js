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
const tween = (dur, fn) => new Promise(res => {
  const t0 = now();
  const step = () => { const t = clamp01((now() - t0) / dur); fn(t); t < 1 ? later(step) : res(); };
  later(step);
});

/* ---------------------------------------------------------------- dom */
const $ = id => document.getElementById(id);
const app = $("app"), skyEl = $("sky"), clip = $("sky-clip"), inner = $("sky-inner"), hl = $("hl");
const sky = new Sky(skyEl);
let view = "home", busy = false, req = null, wiseCanvas = null, overlay = null;
const setView = v => { view = v; app.dataset.view = v; };

/* ---------------------------------------------------------------- helpers */
const MAS = 3.6e6, PX = 6.15;
function propagate(it, year) {
  const dt = year - (it.epoch ?? 2000);
  const dec1 = it.dec + (it.pmdec || 0) * dt / MAS;
  const ra1 = it.ra + (it.pmra || 0) * dt / MAS / Math.cos(((it.dec + dec1) / 2) * Math.PI / 180);
  return [ra1, dec1];
}
const fieldDeg = fov => { let n = Math.ceil(fov / PX); n += n % 2; return n * PX / 3600; };
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
let spinning = false;
function homeView() { return [0, 0, sky.homeFov()]; }
async function boot() {
  await sky.init();
  sky.set(0, 0, sky.homeFov());
  sky.startSpin(2.2); spinning = true;
  bindSky();
  window.addEventListener("resize", () => { if (view === "home") sky.a.setFoV(sky.homeFov()); layoutField(); });
  window.__sky = sky; window.__ready = true;
}

function bindSky() {
  let down = null, moved = 0;
  const rect = () => clip.getBoundingClientRect();
  clip.addEventListener("wheel", e => { e.preventDefault(); e.stopPropagation(); }, { capture: true, passive: false });
  clip.addEventListener("pointerdown", e => {
    if (view !== "home") return;
    if (spinning) { sky.haltSpin(); spinning = false; }          // the cursor takes the universe
    down = { x: e.clientX, y: e.clientY }; moved = 0; hl.classList.add("down");
  }, true);
  window.addEventListener("pointermove", e => {
    if (view !== "home" || app.classList.contains("searching")) return hl.classList.remove("on");
    if (down) { moved = Math.max(moved, Math.hypot(e.clientX - down.x, e.clientY - down.y)); if (moved > 5) hl.classList.remove("on"); }
    if (down && moved > 5) return;
    const r = rect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const w = e.target && clip.contains(e.target) ? sky.pix2world(x, y) : null;
    hl.classList.toggle("on", !!w);
    if (w) hl.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
  });
  clip.addEventListener("pointerleave", () => hl.classList.remove("on"));
  window.addEventListener("pointerup", e => {
    hl.classList.remove("down");
    if (!down) return;
    const wasClick = moved <= 5; down = null;
    if (!wasClick || view !== "home" || busy) return;
    const r = rect(), w = sky.pix2world(e.clientX - r.left, e.clientY - r.top);
    if (w) go({ key: "custom", name: "Selected patch", ra: w[0], dec: w[1], pmra: 0, pmdec: 0, epoch: args.mid, fov: 180 });
  });
}

/* ---------------------------------------------------------------- flight */
const panelA = () => { const r = $("pWise").getBoundingClientRect(); return r.width ? r.height / r.width : 1.2; };
// width of the field on screen at the end of the flight; the panel window (width x width*a) must fit inside the viewport
const SF = () => Math.min(sky.W * 0.4, 0.8 * (sky.H - 20) / panelA());
let nonce = 0;

async function flyTo(ra, dec, fov, dur, { zoomLag = 0.12, panEnd = 0.8 } = {}) {
  const c0 = sky.center(), u0 = unit(c0[0], c0[1]), u1 = unit(ra, dec), f0 = Math.log(sky.fov()), f1 = Math.log(fov);
  await tween(dur, t => {
    const pc = flightEase(t / panEnd), pz = flightEase((t - zoomLag) / (1 - zoomLag));
    const [r, d] = radec(slerp(u0, u1, pc));
    sky.set(r, d, Math.exp(f0 + (f1 - f0) * pz));
    if (overlay) overlay.style.opacity = String(smooth(clamp01((t - 0.86) / 0.14)));
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
  if (busy) return; busy = true;
  closeSearch(true); closeAbout();
  req = Object.assign({}, item, { nonce: String(Date.now()) + Math.random().toString(36).slice(2, 6) });
  req.pos = propagate(item, args.mid);
  req.fieldDeg = fieldDeg(item.fov || 180);
  emit(Object.assign({ type: "go" }, req));
  setView("flying"); sky.haltSpin(); spinning = false;
  wiseCanvas = null; makeOverlay();
  // smooth AllWISE picture of the same field, built in small slices while the flight runs
  const my = req.nonce;
  const cw = 1152, ch = Math.round(cw * sky.H / sky.W);
  renderCutout(SURVEYS.wise, req.pos[0], req.pos[1], req.fieldDeg * sky.W / SF(), cw, ch, { gain: 2.0, gamma: 0.85, black: 0.015 }).then(c => {
    if (!req || req.nonce !== my) return;
    wiseCanvas = c; overlay.width = c.width; overlay.height = c.height; overlay.getContext("2d").drawImage(c, 0, 0);
  }).catch(() => {});
  await flyTo(req.pos[0], req.pos[1], req.fieldDeg * sky.W / SF(), 4600);
  req.arrived = true;
  setView("waiting"); tryReveal();
}

/* ---------------------------------------------------------------- reveal (squish into the left panel) */
function layoutField() {}
async function tryReveal() {
  const res = args.result;
  if (!req || !req.arrived || view === "view" || view === "settling") return;
  if (args.error) { toast(args.error); return abortHome(); }
  if (!res || res.nonce !== req.nonce) return;
  setView("settling");
  fillPage(res);
  await loadFrames(res);
  for (let i = 0; i < 50 && !wiseCanvas; i++) await new Promise(r => setTimeout(r, 60));
  const W = sky.W, H = sky.H, f = $("pWise").getBoundingClientRect(), S = SF(), k = f.width / S, A = f.height / f.width;
  const dx = f.left + f.width / 2 - W / 2, dy = f.top + f.height / 2 - H / 2;
  setTimeout(() => app.classList.add("shown"), 260);
  const win = p => {                                         // the on-screen window of sky at progress p
    const w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
    return `inset(${cy - h / 2}px ${W - cx - w / 2}px ${H - cy - h / 2}px ${cx - w / 2}px round ${26 * smooth(clamp01(p * 1.3))}px)`;
  };
  await tween(1100, t => {
    const p = smooth(t);
    inner.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
    clip.style.clipPath = win(p);
  });
  paintWise(res); $("wiseCv").classList.add("on");
  await new Promise(r => setTimeout(r, 420));
  clip.style.visibility = "hidden";
  setView("view"); startPlayer(res);
}

function abortHome() { busy = false; req = null; leave(true); }

async function leave(fromError = false) {
  if (busy && !fromError && view !== "view" && view !== "waiting") return;
  stopPlayer();
  const res = args.result, was = view;
  emit({ type: "home", nonce: "h" + Date.now() });
  setView("leaving");
  app.classList.remove("shown");
  if (was === "view" || was === "settling") {
    // re-open the sky exactly where it ended, then grow it back out
    const W = sky.W, H = sky.H, f = $("pWise").getBoundingClientRect(), S = SF(), k = f.width / S, A = f.height / f.width;
    const dx = f.left + f.width / 2 - W / 2, dy = f.top + f.height / 2 - H / 2;
    clip.style.visibility = "visible"; $("wiseCv").classList.remove("on");
    await tween(900, t => {
      const p = 1 - smooth(t), w = S * (1 + (k - 1) * p), h = w * A, cx = W / 2 + dx * p, cy = H / 2 + dy * p;
      inner.style.transform = `translate(${dx * p}px,${dy * p}px) scale(${1 + (k - 1) * p})`;
      const grow = smooth(t);                                  // opens out to the full window as it leaves the panel
      const L = (cx - w / 2) * (1 - grow), T = (cy - h / 2) * (1 - grow), Rr = (W - cx - w / 2) * (1 - grow), B = (H - cy - h / 2) * (1 - grow);
      clip.style.clipPath = `inset(${T}px ${Rr}px ${B}px ${L}px round ${26 * (1 - t)}px)`;
    });
  }
  clip.style.clipPath = "inset(0 0 0 0 round 0px)"; inner.style.transform = "none"; clip.style.visibility = "visible";
  if (overlay) { overlay.remove(); overlay = null; }
  setView("flying");
  const c = sky.center();
  await flyTo(c[0], 0, sky.homeFov(), 3200, { zoomLag: 0.0, panEnd: 1 });
  setView("home"); busy = false; req = null; sky.startSpin(2.2); spinning = true;
}

/* ---------------------------------------------------------------- page two content */
function fillPage(res) {
  $("iName").textContent = res.name;
  const [a, b] = sexa(res.ra, res.dec);
  $("iCoord").innerHTML = `${a}<br>${b}`;
  const v = res.verdict;
  $("iVerdict").innerHTML = `<div class="chip tone-${v.tone}"><i></i>${v.word}</div>` +
    `<div class="conf">Confidence <b>${v.conf}</b></div>` +
    `<div class="dots">${v.checks.map(c => `<span class="${c.ok ? "ok" : "no"}"><em></em>${c.k}</span>`).join("")}</div>`;
  $("wiseCap").textContent = "2010";
}
function paintWise(res) {
  const cv = $("wiseCv"), f = $("pWise").getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d);
  const g = cv.getContext("2d"); g.imageSmoothingQuality = "high";
  if (wiseCanvas) {                                  // the same window of sky the flight ended on
    const sw = wiseCanvas.width * SF() / sky.W, sh = sw * f.height / f.width;
    g.drawImage(wiseCanvas, (wiseCanvas.width - sw) / 2, (wiseCanvas.height - sh) / 2, sw, sh, 0, 0, cv.width, cv.height);
  }
  if (req) {                                         // where the target sat when WISE looked (about 2010.4)
    const p = propagate(req, 2010.4), s = res.field_deg * 3600;
    const dx = (p[0] - res.ra_c) * Math.cos(res.dec_c * Math.PI / 180) * 3600, dy = (p[1] - res.dec_c) * 3600;
    ring(g, cv.width * (0.5 - dx / s), cv.height / 2 - cv.width * dy / s, cv.width, 0.9);
  }
}
function ring(g, x, y, size, a = 0.85) {
  if (x < -size * .1 || x > size * 1.1) return;
  g.save(); g.strokeStyle = `rgba(190,120,132,${a})`; g.lineWidth = Math.max(2, size / 240);
  g.beginPath(); g.arc(x, y, size * 0.055, 0, Math.PI * 2); g.stroke(); g.restore();
}

/* ---------------------------------------------------------------- SPHEREx player */
let P = null, imgs = [], playing = true, tpos = 0, dragging = false, raf = 0, last = 0;
function loadFrames(res) {
  return Promise.all(res.frames.map(b => new Promise(r => { const im = new Image(); im.onload = () => r(im); im.onerror = () => r(im); im.src = "data:image/jpeg;base64," + b; }))).then(a => { imgs = a; });
}
function startPlayer(res) {
  P = res; tpos = 0; playing = true; $("play").classList.remove("paused");
  const cv = $("spxCv"), f = $("pSpx").getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(f.width * d); cv.height = Math.round(f.height * d); makeBackdrop(cv);
  last = performance.now(); cancelAnimationFrame(raf); raf = requestAnimationFrame(loop);
}
function stopPlayer() { cancelAnimationFrame(raf); P = null; }
const FPS = 5;
function loop(t) {
  if (!P) return;
  const dt = Math.max(0, Math.min(0.1, (t - last) / 1000)); last = t;
  if (playing && !dragging) tpos = (tpos + dt * FPS + imgs.length) % imgs.length;
  drawSpx(); raf = requestAnimationFrame(loop);
}
let backdrop = null;
function makeBackdrop(cv) {                           // soft, dark extension of the frame so the square sits centred in the tall panel
  backdrop = document.createElement("canvas"); backdrop.width = Math.round(cv.width / 6); backdrop.height = Math.round(cv.height / 6);
  const g = backdrop.getContext("2d"), im = imgs[0], sc = backdrop.height / im.naturalHeight;
  g.filter = "blur(6px) brightness(.45)";
  g.drawImage(im, (backdrop.width - im.naturalWidth * sc) / 2, 0, im.naturalWidth * sc, backdrop.height);
}
function drawSpx() {
  const cv = $("spxCv"), g = cv.getContext("2d"), n = imgs.length; if (!n) return;
  const i = Math.floor(tpos) % n, fr = tpos - Math.floor(tpos), j = (i + 1) % n, e = smooth(fr);
  const cw = cv.width, ch = cv.height, y0 = (ch - cw) / 2;
  g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
  if (backdrop) g.drawImage(backdrop, 0, 0, cw, ch); else { g.fillStyle = "#0d0f12"; g.fillRect(0, 0, cw, ch); }
  g.drawImage(imgs[i], 0, y0, cw, cw);
  if (e > 0.003) { g.globalAlpha = e; g.drawImage(imgs[j], 0, y0, cw, cw); g.globalAlpha = 1; }
  const a = P.marks[i], b = P.marks[j];
  if (a && b) ring(g, (a[0] + (b[0] - a[0]) * e) * cw, y0 + (a[1] + (b[1] - a[1]) * e) * cw, cw, 0.9);
  else if (a) ring(g, a[0] * cw, y0 + a[1] * cw, cw, 0.9);
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
  closeAbout(); sEl.classList.add("open"); app.classList.add("searching"); sbtn.setAttribute("aria-expanded", "true");
  hl.classList.remove("on"); showItems(presetItems()); setTimeout(() => qEl.focus(), 120);
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
function choose(it) { go(Object.assign({ fov: 180, epoch: 2000, pmra: 0, pmdec: 0 }, it)); }

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
  const local = presetItems().filter(p => p.name.toLowerCase().includes(q.toLowerCase()));
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
async function remote(q, my, have) {
  const ctl = new AbortController(); setTimeout(() => ctl.abort(), 5000);
  const cap = q[0].toUpperCase() + q.slice(1), vars = [...new Set([q, cap, q.toUpperCase()])];
  const like = vars.flatMap(v => [`'${esc(v)}%'`, `'NAME ${esc(v)}%'`, `'* ${esc(v)}%'`, `'V* ${esc(v)}%'`]).map(p => `id LIKE ${p}`).join(" OR ");
  const adql = `SELECT TOP 8 main_id, ra, dec, pmra, pmdec, otype FROM basic JOIN ident ON oid = oidref WHERE ${like}`;
  let out = [];
  try {
    const r = await fetch("https://simbad.cds.unistra.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=json&query=" + encodeURIComponent(adql), { signal: ctl.signal });
    const j = await r.json();
    out = (j.data || []).filter(x => x[1] != null).map(x => ({ key: "custom", name: String(x[0]).replace(/^NAME\s+/, "").replace(/^\*\s+/, ""), ra: x[1], dec: x[2], pmra: x[3] || 0, pmdec: x[4] || 0, epoch: 2000, kind: OTYPE[x[5]] || x[5] || "" }));
  } catch (e) { /* fall through to Sesame */ }
  if (!out.length) {
    try {
      const r = await fetch("https://vizier.cfa.harvard.edu/viz-bin/nph-sesame/-oI/SNV?" + encodeURIComponent(q), { signal: ctl.signal });
      const t = await r.text(), J = t.match(/%J\s+([\d.]+)\s+([+-][\d.]+)/), P2 = t.match(/%P\s+([-\d.]+)\s+([-\d.]+)/), N = t.match(/%I\.0\s+(.+)/), C = t.match(/%C\.0\s+(\S+)/);
      if (J) out = [{ key: "custom", name: (N ? N[1] : q).trim().replace(/^NAME\s+/, ""), ra: +J[1], dec: +J[2], pmra: P2 ? +P2[1] : 0, pmdec: P2 ? +P2[2] : 0, epoch: 2000, kind: C ? (OTYPE[C[1]] || C[1]) : "" }];
    } catch (e) { /* offline: presets still work */ }
  }
  if (my !== qid || !sEl.classList.contains("open")) return;
  const seen = new Set(have.map(h => h.name.toLowerCase())); const merged = have.concat(out.filter(o => !seen.has(o.name.toLowerCase()))).slice(0, 8);
  if (merged.length) showItems(merged);
}

/* ---------------------------------------------------------------- top bar */
function openAbout() { if (busy && view !== "view") return; closeSearch(true); app.classList.add("about"); }
function closeAbout() { app.classList.remove("about"); }
$("aboutBtn").onclick = () => (app.classList.contains("about") ? closeAbout() : openAbout());
$("brand").onclick = () => { closeSearch(true); if (app.classList.contains("about")) closeAbout(); if (view === "view" || view === "waiting") leave(); };

function onArgs() { tryReveal(); }
boot();
