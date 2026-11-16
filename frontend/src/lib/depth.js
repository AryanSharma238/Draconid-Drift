// "Past & future": the patch of sky as a 3D volume. Distance from Earth runs along the axis on a log scale, with
// the scale drawn on the axis itself so it reads true from any angle; turn the camera freely in 3D. Galaxies are
// drawn at their measured angular size, shape and tilt (on the same scale as the patch's width); stars are points.
// The time slider moves every star along its measured 3D velocity (across the sky and toward or away from us),
// backwards or forwards, and in the deep past sweeps the light-cone slice outwards.
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
// the star's position vector in its own frame (east, north, along the line of sight) is (0, 0, d); after t years
// it is (ve t, vn t, d + vr t). Its new distance is the length of that vector and its new direction on the sky
// is the angle of the vector, added to where it started.
function moved(o, t) {
  if (o.cls !== "star" || !t) return { x: o.x, y: o.y, ly: o.ly };
  const tt = Math.max(-MOTION_MAX, Math.min(MOTION_MAX, t));
  const e = o.v[0] * KMS_LY * tt, n = o.v[1] * KMS_LY * tt, r = o.ly + o.v[2] * KMS_LY * tt;
  return { x: o.x + Math.atan2(e, r) * 57.29578, y: o.y + Math.atan2(n, Math.hypot(r, e)) * 57.29578, ly: Math.max(0.05, Math.hypot(e, n, r)) };
}

export async function open(el, data, res) {
  if (!T) T = await import(/* @vite-ignore */ THREE_URL);
  close();
  const cv = el.querySelector("#depthCv"), ruler = el.querySelector("#rulerCv"), tip = el.querySelector("#depthTip"), tags = el.querySelector("#depthTags");
  const renderer = new T.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new T.Scene(), cam = new T.OrthographicCamera(-1, 1, 1, -1, -1000, 1000);
  const R = data.radius_deg || 0.15;
  const world = new T.Group(); scene.add(world);
  // sky offsets (deg east, deg north) + distance -> world: x = depth, y = north, z = east
  const unit = s => HALF * Math.max(s, 0.25) / R;           // world units per degree of sky at depth s
  const place = (x, y, ly) => { const s = sOf(ly) * DEC, k = unit(s); return new T.Vector3(s, y * k, x * k); };

  // the patch: a pyramid from Earth, with a frame at each power of ten
  const smax = 10.3 * DEC, edge = new T.LineBasicMaterial({ color: 0x8a5a62, transparent: true, opacity: .2 });
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
  const objs = (data.objects || []).filter(o => Math.abs(o.x) <= R * 1.02 + (o.size || 0) / 120 && Math.abs(o.y) <= R * 1.02 + (o.size || 0) / 120).map(o => {   // only what is inside this patch
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
  const trails = new T.LineSegments(trailGeo, new T.LineBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: .32 })); world.add(trails);
  const shellMat = new T.MeshBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: 0, side: T.DoubleSide, depthWrite: false });
  const shell = new T.Mesh(new T.PlaneGeometry(1, 1), shellMat); shell.rotation.y = Math.PI / 2; world.add(shell);

  // labels for the things that matter: Earth, the target, the best-known names, quasars, the nearest star
  const ranked = objs.slice().sort((a, b) => (b.userData.o.nb || 0) - (a.userData.o.nb || 0)).filter(sp => !/^Gaia DR3/.test(sp.userData.o.name));
  const nearest = objs.filter(sp => sp.userData.o.cls === "star").sort((a, b) => a.userData.o.ly - b.userData.o.ly)[0];
  const labelled = [...new Set([target, nearest, ...objs.filter(sp => sp.userData.o.cls === "quasar").slice(0, 2), ...ranked.slice(0, 6)].filter(Boolean))].slice(0, 9);
  tags.innerHTML = "";
  function mk(a, b, cls) { const d = document.createElement("div"); d.className = "dtag " + cls; d.innerHTML = `<b>${esc(a)}</b><span>${esc(b)}</span><em></em>`; tags.appendChild(d); return d; }
  const tagEls = [{ el: mk("Earth", "you are here", "earth"), obj: earth }]
    .concat(labelled.map(sp => ({ el: mk(sp.userData.o.name, fmtLy(sp.userData.o.ly), sp === target ? "tgt" : ""), obj: sp })));

  // view: a free 3D camera that orbits a point on the distance axis. Drag to turn in any direction (all three
  // axes), shift-drag or right-drag to slide along the distance axis, scroll to zoom. The distance scale is drawn
  // on the axis itself, so it stays true from every angle.
  const ss = objs.map(sp => sOf(sp.userData.o.ly)), lo = ss.length ? Math.min(...ss) : 0, hi = ss.length ? Math.max(...ss) : 4;
  // start with everything in view: Earth at the near end, the farthest object at the far end, with a margin
  const far = Math.min(10.3, hi + 0.5) * DEC;
  let cx = far / 2, span = Math.max(3 * DEC, far * 1.18 + 1.2);
  let yaw = 0.42, pitch = 0.26, roll = 0.5, time = 0, W = 1, H = 1;   // start turned a little, so it reads as 3D
  const size = () => { const r = el.getBoundingClientRect(); W = r.width; H = r.height; renderer.setSize(W, H, false); };
  size(); const ro = new ResizeObserver(size); ro.observe(el);
  setTimeout(size, 120); setTimeout(size, 600);              // the panel may still be settling when the view opens
  let drag = null;
  cv.addEventListener("contextmenu", e => e.preventDefault());
  cv.addEventListener("pointerdown", e => { e.preventDefault(); drag = [e.clientX, e.clientY, cx, yaw, pitch, e.shiftKey || e.button === 2]; cv.setPointerCapture(e.pointerId); tip.classList.remove("on"); });
  cv.addEventListener("pointerup", () => { drag = null; });
  cv.addEventListener("pointermove", e => {
    if (drag) {
      const dx = e.clientX - drag[0], dy = e.clientY - drag[1];
      if (drag[5]) cx = Math.max(-2, Math.min(11 * DEC, drag[2] - dx / W * span * Math.cos(yaw)));
      else { yaw = drag[3] + dx * 0.006; pitch = Math.max(-1.5, Math.min(1.5, drag[4] + dy * 0.006)); }
      return;
    }
    hover(e);
  });
  cv.addEventListener("wheel", e => { e.preventDefault(); span = Math.max(1.2, Math.min(12 * DEC, span * Math.exp(e.deltaY * 0.0012))); }, { passive: false });
  cv.addEventListener("dragstart", e => e.preventDefault());
  // the distance axis with its scale: a line from Earth, tick marks, labels that follow it in 3D
  const axisMat = new T.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: .45 });
  world.add(new T.Line(new T.BufferGeometry().setFromPoints([new T.Vector3(0, 0, 0), new T.Vector3(10.3 * DEC, 0, 0)]), axisMat));
  const ticks = [];
  for (let k = 0; k <= 10; k++) for (const m of k < 10 ? [1, 2, 5] : [1]) {
    const ly = m * 10 ** k, x = sOf(ly) * DEC, h = m === 1 ? .7 : .35;
    world.add(new T.Line(new T.BufferGeometry().setFromPoints([new T.Vector3(x, -h, 0), new T.Vector3(x, h, 0)]), axisMat));
    const d = document.createElement("div"); d.className = "dtick" + (m === 1 ? " major" : "");
    d.textContent = fmtLy(ly).replace(" thousand", "k").replace(" million", "M").replace(" billion", "B");
    tags.appendChild(d); const o = new T.Object3D(); o.position.set(x, -h, 0); world.add(o); ticks.push({ el: d, obj: o, major: m === 1 });
  }
  const v3 = new T.Vector3();
  const scr = obj => { obj.getWorldPosition(v3); v3.project(cam); return { x: (v3.x + 1) / 2 * W, y: (1 - v3.y) / 2 * H }; };
  function hover(e) {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top; let best = null, bd = 16;
    const rv = o => o.v && o.v[2] ? "" : o.cls === "star" ? " · no radial speed measured (motion across the sky only)" : "";
    for (const sp of objs) { const p = scr(sp); const d = Math.hypot(p.x - mx, p.y - my); if (d < bd) { bd = d; best = sp; } }
    if (best) { const o = best.userData.o; tip.innerHTML = `<b>${esc(o.name)}</b><span>${esc(o.kind)} · ${fmtLy(best.userData.now)}${o.size ? ` · ${nice(o.size, 1)}′ across` : ""}${rv(o)}</span>`;
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
  function drawRuler() {
    let last = null;
    for (const t of ticks) {
      const p = scr(t.obj), on = p.x > -30 && p.x < W + 30 && p.y > -20 && p.y < H + 20;
      const room = !last || Math.hypot(p.x - last.x, p.y - last.y) > (t.major ? 46 : 64);
      if (on && room) { t.el.style.display = "block"; t.el.style.transform = `translate(${p.x}px,${p.y + 6}px) translateX(-50%)`; last = p; }
      else t.el.style.display = "none";
    }
  }
  function placeTags() {
    const used = [];
    for (const t of tagEls) {
      const o = t.obj.userData && t.obj.userData.o;
      if (o) {                                         // distance now, and how much it has changed
        const now = t.obj.userData.now, dl = now - o.ly, sp = t.el.children[1], em = t.el.children[2];
        const txt = fmtLy(now); if (sp.textContent !== txt) sp.textContent = txt;
        const dtx = time && Math.abs(dl) >= Math.max(0.005, o.ly * 1e-6) ? `${dl > 0 ? "+" : "−"}${fmtLy(Math.abs(dl))}` : "";
        if (em.textContent !== dtx) { em.textContent = dtx; em.className = dl > 0 ? "far" : "near"; }
      }
      const p = scr(t.obj); let y = p.y - 30;
      if (p.x < -40 || p.x > W + 40 || p.y < -20 || p.y > H) { t.el.style.display = "none"; continue; }
      for (let n = 0; n < 6; n++) { const hit = used.find(u => Math.abs(u.x - p.x) < 150 && Math.abs(u.y - y) < 30); if (!hit) break; y = hit.y - 32; }
      used.push({ x: p.x, y }); t.el.style.display = "block"; t.el.style.transform = `translate(${p.x + 8}px,${y}px)`;
    }
  }
  let raf = 0;
  const loop = () => {
    const a = W / H; cam.left = -span / 2; cam.right = span / 2; cam.top = span / 2 / a; cam.bottom = -span / 2 / a;
    const R0 = 150;                                     // orbit the focus point on the axis
    cam.position.set(cx + R0 * Math.sin(yaw) * Math.cos(pitch), R0 * Math.sin(pitch), R0 * Math.cos(yaw) * Math.cos(pitch));
    cam.up.set(0, 1, 0); cam.lookAt(cx, 0, 0); cam.updateProjectionMatrix();
    world.rotation.x = roll;
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
