// "Past & future": the patch of sky seen from the side. Distance from Earth runs left to right on a log scale,
// read off the ruler along the bottom; the ruler is exact for every object because the view is orthographic and
// only ever spins about the line of sight. Galaxies are drawn at their measured angular size, shape and tilt (on
// the same scale as the patch's width); stars are points. The time slider moves every star along its measured
// 3D velocity, backwards or forwards, and in the deep past sweeps the light-cone slice outwards.
const THREE_URL = "https://unpkg.com/three@0.160.0/build/three.module.js";
let T = null;

const DEC = 10;                    // world units per factor of 10 in distance
const HALF = 0.42;                 // drawn half-width of the patch, as a fraction of depth (the real patch is ~0.3 deg)
const KMS_LY = 1 / 299792.458;     // km/s -> light years per year
const MOTION_MAX = 1e5;            // years: beyond this a straight-line path is no longer meaningful
const sOf = ly => Math.log10(Math.max(ly, 1));
const nice = (v, d) => (Math.abs(v - Math.round(v)) < 1e-6 ? String(Math.round(v)) : v.toFixed(d));
export const fmtLy = ly => ly < 1e3 ? `${ly < 10 ? nice(ly, 1) : Math.round(ly).toLocaleString()} ly` : ly < 1e6 ? `${nice(ly / 1e3, ly < 1e4 ? 1 : 0)} thousand ly`
  : ly < 1e9 ? `${nice(ly / 1e6, ly < 1e7 ? 1 : 0)} million ly` : `${nice(ly / 1e9, 1)} billion ly`;
const fmtYears = y => y < 1e3 ? `${Math.round(y).toLocaleString()} years` : y < 1e6 ? `${nice(y / 1e3, y < 1e4 ? 1 : 0)} thousand years`
  : y < 1e9 ? `${nice(y / 1e6, y < 1e7 ? 1 : 0)} million years` : `${nice(y / 1e9, 1)} billion years`;
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const sameName = (a, b) => { const x = norm(a), y = norm(b); return x && y && (x === y || x.includes(y) || y.includes(x)); };

let st = null;

// where a star is at time t (years from now): r(t) = r0 + v t, straight line, both directions
function moved(o, t) {
  if (o.cls !== "star" || !t) return { x: o.x, y: o.y, ly: o.ly };
  const tt = Math.max(-MOTION_MAX, Math.min(MOTION_MAX, t));
  const ve = o.v[0] * KMS_LY * tt, vn = o.v[1] * KMS_LY * tt, vr = o.v[2] * KMS_LY * tt;
  return { x: o.x + (ve / o.ly) * 57.29578, y: o.y + (vn / o.ly) * 57.29578, ly: Math.max(0.05, Math.hypot(o.ly + vr, ve, vn)) };
}

export async function open(el, data, res) {
  if (!T) T = await import(THREE_URL);
  close();
  const cv = el.querySelector("#depthCv"), ruler = el.querySelector("#rulerCv"), tip = el.querySelector("#depthTip"), tags = el.querySelector("#depthTags");
  const renderer = new T.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new T.Scene(), cam = new T.OrthographicCamera(-1, 1, 1, -1, -500, 500);
  const R = data.radius_deg || 0.15;
  const world = new T.Group(); scene.add(world);            // spins about the depth (x) axis only
  // sky offsets (deg east, deg north) + distance -> world: x = depth, y = north, z = east
  const unit = s => HALF * Math.max(s, 0.25) / R;           // world units per degree of sky at depth s
  const place = (x, y, ly) => { const s = sOf(ly) * DEC, k = unit(s); return new T.Vector3(s, y * k, x * k); };

  // the patch: a pyramid from Earth, with a frame at each power of ten
  const smax = 10.3 * DEC, edge = new T.LineBasicMaterial({ color: 0x8d5760, transparent: true, opacity: .6 });
  for (const [a, b] of [[-1, -1], [1, -1], [1, 1], [-1, 1]])
    world.add(new T.Line(new T.BufferGeometry().setFromPoints([new T.Vector3(0, 0, 0), new T.Vector3(smax, a * HALF * smax, b * HALF * smax)]), edge));
  for (let k = 1; k <= 10; k++) {
    const s = k * DEC, w = HALF * s;
    world.add(new T.LineLoop(new T.BufferGeometry().setFromPoints([new T.Vector3(s, -w, -w), new T.Vector3(s, w, -w), new T.Vector3(s, w, w), new T.Vector3(s, -w, w)]),
      new T.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: .07 })));
  }
  const earth = new T.Mesh(new T.SphereGeometry(.45, 24, 16), new T.MeshBasicMaterial({ color: 0x5b8fd9 })); world.add(earth);

  // objects: stars as points (no star is resolvable at these distances); galaxies at their measured size, shape, tilt
  const tex = (stops, n) => { const c = document.createElement("canvas"); c.width = c.height = n; const x = c.getContext("2d");
    const g = x.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2); stops.forEach(([o, col]) => g.addColorStop(o, col));
    x.fillStyle = g; x.fillRect(0, 0, n, n); return new T.CanvasTexture(c); };
  const dot = tex([[0, "#fff"], [.22, "rgba(255,255,255,.55)"], [1, "rgba(255,255,255,0)"]], 64);
  const disc = tex([[0, "rgba(255,245,235,1)"], [.18, "rgba(220,200,255,.75)"], [.7, "rgba(170,140,240,.25)"], [1, "rgba(0,0,0,0)"]], 128);
  const COL = { star: 0xffe6c8, galaxy: 0xd8c4ff, quasar: 0x8fd0ff };
  const objs = (data.objects || []).map(o => {
    const big = o.cls !== "star" && o.size;
    const m = new T.SpriteMaterial({ map: big ? disc : dot, color: COL[o.cls] || COL.star, transparent: true, depthWrite: false, blending: T.AdditiveBlending });
    const sp = new T.Sprite(m);
    sp.userData = { o, big, px: o.cls === "star" ? 7 + Math.max(0, 14 - (o.mag ?? 12)) * 0.9 : o.cls === "quasar" ? 11 : 9, now: o.ly };
    if (big) m.rotation = ((o.pa ?? 0) + 90) * Math.PI / 180;
    world.add(sp); return sp;
  });
  const target = (res.facts && res.facts.kind) ? objs.find(sp => sameName(sp.userData.o.name, res.name)) : null;
  const trailPos = new Float32Array(Math.max(1, objs.length) * 6), trailGeo = new T.BufferGeometry();
  trailGeo.setAttribute("position", new T.BufferAttribute(trailPos, 3));
  const trails = new T.LineSegments(trailGeo, new T.LineBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: .75 })); world.add(trails);
  const shellMat = new T.MeshBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: 0, side: T.DoubleSide, depthWrite: false });
  const shell = new T.Mesh(new T.PlaneGeometry(1, 1), shellMat); shell.rotation.y = Math.PI / 2; world.add(shell);

  // labels for the things that matter: Earth, the target, the best-known names, quasars, the nearest star
  const ranked = objs.slice().sort((a, b) => (b.userData.o.nb || 0) - (a.userData.o.nb || 0)).filter(sp => !/^Gaia DR3/.test(sp.userData.o.name));
  const nearest = objs.filter(sp => sp.userData.o.cls === "star").sort((a, b) => a.userData.o.ly - b.userData.o.ly)[0];
  const labelled = [...new Set([target, nearest, ...objs.filter(sp => sp.userData.o.cls === "quasar").slice(0, 2), ...ranked.slice(0, 6)].filter(Boolean))].slice(0, 9);
  tags.innerHTML = "";
  function mk(a, b, cls) { const d = document.createElement("div"); d.className = "dtag " + cls; d.innerHTML = `<b>${esc(a)}</b><span>${esc(b)}</span>`; tags.appendChild(d); return d; }
  const tagEls = [{ el: mk("Earth", "you are here", "earth"), obj: earth }]
    .concat(labelled.map(sp => ({ el: mk(sp.userData.o.name, fmtLy(sp.userData.o.ly), sp === target ? "tgt" : ""), obj: sp })));

  // view: side-on, Earth at the left. Drag sideways to pan along distance, up/down to spin the patch about
  // its axis; scroll to zoom where the pointer is. The ruler stays exact in every position.
  const ss = objs.map(sp => sOf(sp.userData.o.ly)), lo = ss.length ? Math.min(...ss) : 0, hi = ss.length ? Math.max(...ss) : 4;
  let cx = (Math.max(0, lo - 0.6) + Math.min(10.3, hi + 0.4)) / 2 * DEC, span = Math.max(3 * DEC, Math.min(10.8, hi - Math.max(0, lo - 0.6) + 1.2) * DEC);
  cx = Math.max(cx, span / 2 - 0.08 * span);               // keep Earth just inside the left edge at the start
  let spin = 0.5, time = 0, W = 1, H = 1;
  const size = () => { const r = el.getBoundingClientRect(), d = Math.min(2, devicePixelRatio || 1); W = r.width; H = r.height;
    renderer.setSize(W, H, false); ruler.width = W * d; ruler.height = 46 * d; };
  size(); const ro = new ResizeObserver(size); ro.observe(el);
  const toWorldX = px => cx + (px / W - 0.5) * span;
  let drag = null;
  cv.addEventListener("pointerdown", e => { e.preventDefault(); drag = [e.clientX, e.clientY, cx, spin]; cv.setPointerCapture(e.pointerId); tip.classList.remove("on"); });
  cv.addEventListener("pointerup", () => { drag = null; });
  cv.addEventListener("pointermove", e => {
    if (drag) { cx = Math.max(-2, Math.min(11 * DEC, drag[2] - (e.clientX - drag[0]) / W * span)); spin = drag[3] + (e.clientY - drag[1]) * 0.008; return; }
    hover(e);
  });
  cv.addEventListener("wheel", e => { e.preventDefault();
    const r = cv.getBoundingClientRect(), px = e.clientX - r.left, before = toWorldX(px);
    span = Math.max(1.2, Math.min(12 * DEC, span * Math.exp(e.deltaY * 0.0012)));
    cx += before - toWorldX(px);                               // zoom about the pointer
  }, { passive: false });
  cv.addEventListener("dragstart", e => e.preventDefault());
  const v3 = new T.Vector3();
  const scr = obj => { obj.getWorldPosition(v3); v3.project(cam); return { x: (v3.x + 1) / 2 * W, y: (1 - v3.y) / 2 * H }; };
  function hover(e) {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top; let best = null, bd = 16;
    for (const sp of objs) { const p = scr(sp); const d = Math.hypot(p.x - mx, p.y - my); if (d < bd) { bd = d; best = sp; } }
    if (best) { const o = best.userData.o; tip.innerHTML = `<b>${esc(o.name)}</b><span>${esc(o.kind)} · ${fmtLy(best.userData.now)}${o.size ? ` · ${nice(o.size, 1)}′ across` : ""}</span>`;
      tip.style.left = (mx + 14) + "px"; tip.style.top = (my + 10) + "px"; tip.classList.add("on"); }
    else tip.classList.remove("on");
  }

  function layout() {
    let i = 0;
    const pxw = span / W;                                     // world units per screen pixel
    for (const sp of objs) {
      const o = sp.userData.o, m = moved(o, time), p0 = place(o.x, o.y, o.ly), p = place(m.x, m.y, m.ly);
      sp.userData.now = m.ly; sp.position.copy(p);
      trailPos.set([p0.x, p0.y, p0.z, p.x, p.y, p.z], i++ * 6);
      if (sp.userData.big) {                                  // true angular size, on the patch's own scale
        const k = unit(sOf(m.ly) * DEC), w = Math.max(k * o.size / 60, sp.userData.px * pxw), h = Math.max(w * (o.ratio || 1), 3 * pxw);
        sp.scale.set(w * 1.6, h * 1.6, 1);
      } else sp.scale.setScalar(sp.userData.px * pxw);
      sp.material.opacity = time < -MOTION_MAX ? (Math.abs(sOf(o.ly) - sOf(-time)) < 0.12 ? 1 : 0.25) : 1;
    }
    trailGeo.attributes.position.needsUpdate = true; trails.visible = time !== 0;
    if (time < -MOTION_MAX) { const s = sOf(-time) * DEC, w = HALF * s * 2.2; shell.position.set(s, 0, 0); shell.scale.set(w, w, 1); shellMat.opacity = .13; }
    else shellMat.opacity = 0;
  }
  function drawRuler() {                                      // distance from Earth: exact under every object
    const g = ruler.getContext("2d"), d = ruler.width / W;
    g.clearRect(0, 0, ruler.width, ruler.height);
    g.strokeStyle = "rgba(255,255,255,.4)"; g.lineWidth = d; g.beginPath(); g.moveTo(0, 8 * d); g.lineTo(ruler.width, 8 * d); g.stroke();
    g.font = `${11.5 * d}px Montserrat, sans-serif`; g.fillStyle = "rgba(255,255,255,.85)"; g.textAlign = "center";
    const px = s => ((s - cx) / span + 0.5) * W * d;
    let last = -1e9;
    for (let k = 0; k <= 10; k++) for (const m of [1, 2, 5]) {
      if (k === 10 && m > 1) continue;
      const ly = m * 10 ** k, x = px(sOf(ly) * DEC), major = m === 1;
      if (x < -20 || x > ruler.width + 20) continue;
      g.beginPath(); g.moveTo(x, 8 * d); g.lineTo(x, (major ? 16 : 12) * d); g.stroke();
      const lab = fmtLy(ly).replace(" thousand", "k").replace(" million", "M").replace(" billion", "B");
      if (x - last > 64 * d && (major || span < 4 * DEC)) { g.fillText(lab, x, 31 * d); last = x; }
    }
  }
  function placeTags() {
    const used = [];
    for (const t of tagEls) {
      const p = scr(t.obj); let y = p.y - 30;
      if (p.x < -40 || p.x > W + 40 || p.y < -20 || p.y > H) { t.el.style.display = "none"; continue; }
      for (let n = 0; n < 6; n++) { const hit = used.find(u => Math.abs(u.x - p.x) < 150 && Math.abs(u.y - y) < 30); if (!hit) break; y = hit.y - 32; }
      used.push({ x: p.x, y }); t.el.style.display = "block"; t.el.style.transform = `translate(${p.x + 8}px,${y}px)`;
    }
  }
  let raf = 0;
  const loop = () => {
    const a = W / H; cam.left = -span / 2; cam.right = span / 2; cam.top = span / 2 / a; cam.bottom = -span / 2 / a;
    cam.position.set(cx, 0, 100); cam.lookAt(cx, 0, 0); cam.updateProjectionMatrix();
    world.rotation.x = spin;
    layout(); renderer.render(scene, cam); drawRuler(); placeTags();
    raf = requestAnimationFrame(loop);
  };
  loop();
  st = { renderer, ro, raf: () => raf, setTime: t => { time = t; }, data, target: target && target.userData.o };
  return st;
}

export function setTime(t) { if (st) st.setTime(t); }
export function close() { if (!st) return; cancelAnimationFrame(st.raf()); st.ro.disconnect(); st.renderer.dispose(); st = null; }

// slider (-1000..1000) <-> years from now. Log scale both ways with "Today" in the middle.
const PAST = Math.log10(1.38e10), FUT = Math.log10(MOTION_MAX);
export function sliderYears(v) { if (Math.abs(v) < 8) return 0; const f = (Math.abs(v) - 8) / 992; return v < 0 ? -(10 ** (f * PAST)) : 10 ** (f * FUT); }
export function sliderPos(y) { if (!y) return 0; const f = Math.log10(Math.abs(y)) / (y < 0 ? PAST : FUT); return Math.sign(y) * (8 + f * 992); }
export const TICKS = [[-1.38e10, "13.8 billion years ago"], [-1e6, "1 million"], [-1e3, "1,000"], [-10, "10"], [0, "Today"],
  [100, "+100"], [1e3, "+1,000"], [1e4, "+10,000"], [1e5, "+100,000 years"]];

export function timeLabel(t, data, res) {
  if (!t) return "Today";
  const span = fmtYears(Math.abs(t)), tgt = st && st.target;
  const where = tgt && tgt.cls === "star" && Math.abs(t) <= MOTION_MAX ? ` · ${esc(res.name)} ${t > 0 ? "will be" : "was"} ${fmtLy(moved(tgt, t).ly)} away` : "";
  if (t < -MOTION_MAX) {
    const n = ((data && data.objects) || []).filter(o => Math.abs(Math.log10(o.ly) - Math.log10(-t)) < 0.12).length;
    return `${span} ago · light from the glowing slice left then${n ? ` · ${n} object${n > 1 ? "s" : ""}` : ""}`;
  }
  return (t > 0 ? `In ${span}` : `${span} ago`) + where;
}
