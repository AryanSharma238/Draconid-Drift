// "Past & future": the patch of sky as a 3D volume. Every star (Gaia parallax) and galaxy (redshift) sits at its
// real distance along its real line of sight; depth runs on a log scale so 4 light years and 10 billion share one
// picture. A ruler on the left reads light years from Earth wherever you fly. The time slider sweeps the light-cone
// shell into the past, or moves the stars along their measured 3D velocities into the future.
const THREE_URL = "https://unpkg.com/three@0.160.0/build/three.module.js";
let T = null;

const DEC = 12;                    // world units per factor of 10 in distance
const HALF_ANGLE = 0.32;           // drawn half-width of the field cone (radians); the real field is ~0.15 deg wide
const KMS_LY = 1 / 299792.458;     // km/s -> light years per year
const sOf = ly => Math.log10(Math.max(ly, 1));        // depth coordinate, 0 at 1 ly
const nice = (v, d) => (Math.abs(v - Math.round(v)) < 1e-6 ? String(Math.round(v)) : v.toFixed(d));
export const fmtLyShort = ly => ly < 1e3 ? `${ly < 10 ? nice(ly, 1) : Math.round(ly)} ly` : ly < 1e6 ? `${nice(ly / 1e3, ly < 1e4 ? 1 : 0)} thousand ly`
  : ly < 1e9 ? `${nice(ly / 1e6, ly < 1e7 ? 1 : 0)} million ly` : `${nice(ly / 1e9, 1)} billion ly`;

let st = null;                     // live viewer state

export async function open(el, data, res) {
  if (!T) T = await import(THREE_URL);
  close();
  const cv = el.querySelector("#depthCv"), ruler = el.querySelector("#rulerCv"), tip = el.querySelector("#depthTip");
  const renderer = new T.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new T.Scene(), cam = new T.PerspectiveCamera(50, 1, 0.05, 4000);
  const R = data.radius_deg || 0.15;
  // a point at offsets (x east, y north, degrees) and distance ly -> world position (Earth at the origin, looking -z)
  const place = (x, y, ly) => {
    const s = sOf(ly) * DEC, w = Math.tan(HALF_ANGLE) * Math.max(s, 0.4) / R;
    return new T.Vector3(-x * w, y * w, -s);
  };
  // the field's cone, with a frame at every power of ten
  const g = new T.Group(), lineMat = new T.LineBasicMaterial({ color: 0x7a4a51, transparent: true, opacity: .55 });
  const smax = 10.3 * DEC, wmax = Math.tan(HALF_ANGLE) * smax;
  for (const [a, b] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) g.add(new T.Line(new T.BufferGeometry().setFromPoints([new T.Vector3(0, 0, 0), new T.Vector3(a * wmax, b * wmax, -smax)]), lineMat));
  for (let k = 1; k <= 10; k++) {
    const s = k * DEC, w = Math.tan(HALF_ANGLE) * s;
    g.add(new T.LineLoop(new T.BufferGeometry().setFromPoints([new T.Vector3(-w, -w, -s), new T.Vector3(w, -w, -s), new T.Vector3(w, w, -s), new T.Vector3(-w, w, -s)]),
      new T.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: k % 3 === 0 ? .12 : .05 })));
  }
  scene.add(g);
  const earth = new T.Mesh(new T.SphereGeometry(.35, 24, 16), new T.MeshBasicMaterial({ color: 0x5b8fd9 }));
  scene.add(earth);
  // objects
  const sprite = (inner, outer) => { const c = document.createElement("canvas"); c.width = c.height = 64; const x = c.getContext("2d");
    const gr = x.createRadialGradient(32, 32, 0, 32, 32, 32); gr.addColorStop(0, inner); gr.addColorStop(.25, outer); gr.addColorStop(1, "rgba(0,0,0,0)");
    x.fillStyle = gr; x.fillRect(0, 0, 64, 64); const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace; return t; };
  const TEX = { star: sprite("#fff", "rgba(255,226,190,.55)"), galaxy: sprite("#f3e3ff", "rgba(190,150,255,.45)"), quasar: sprite("#fff", "rgba(120,200,255,.7)") };
  const objs = (data.objects || []).map(o => {
    const m = new T.SpriteMaterial({ map: TEX[o.cls] || TEX.star, transparent: true, depthWrite: false, blending: T.AdditiveBlending });
    const sp = new T.Sprite(m), base = o.cls === "galaxy" ? 1.3 : o.cls === "quasar" ? 1.4 : 0.9;
    sp.userData = { o, base }; sp.scale.setScalar(base); scene.add(sp); return sp;
  });
  // the target (if it has a distance): a ring that always faces you
  const tgt = (data.objects || []).find(o => res.facts && res.facts.kind && sameName(o.name, res.name)) || null;
  const ringTex = (() => { const c = document.createElement("canvas"); c.width = c.height = 64; const x = c.getContext("2d");
    x.strokeStyle = "#e8be8c"; x.lineWidth = 4; x.beginPath(); x.arc(32, 32, 26, 0, 7); x.stroke(); return new T.CanvasTexture(c); })();
  const ring = new T.Sprite(new T.SpriteMaterial({ map: ringTex, transparent: true, depthWrite: false }));
  ring.scale.setScalar(2.6); ring.visible = !!tgt; scene.add(ring);
  // the light-cone shell for the past slider, and motion trails for the future
  const shellMat = new T.MeshBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: 0, side: T.DoubleSide, depthWrite: false });
  const shell = new T.Mesh(new T.PlaneGeometry(1, 1), shellMat); scene.add(shell);
  const trailGeo = new T.BufferGeometry(), trailPos = new Float32Array(objs.length * 6);
  trailGeo.setAttribute("position", new T.BufferAttribute(trailPos, 3));
  const trails = new T.LineSegments(trailGeo, new T.LineBasicMaterial({ color: 0xe8be8c, transparent: true, opacity: .7 })); scene.add(trails);

  // camera: orbit around a focus point on the axis; scrolling flies the focus along the depth axis
  const far = objs.length ? Math.max(...objs.map(s => sOf(s.userData.o.ly))) : 3;
  let focus = Math.min(far, 3.5) * DEC * 0.6, yaw = 0.75, pitch = 0.32, dist = Math.max(18, focus * 1.15), time = 0;
  const size = () => { const r = el.getBoundingClientRect(), d = Math.min(2, devicePixelRatio || 1);
    renderer.setSize(r.width, r.height, false); cam.aspect = r.width / r.height; cam.updateProjectionMatrix();
    ruler.width = 120 * d; ruler.height = r.height * d; ruler.style.height = r.height + "px"; };
  size(); const ro = new ResizeObserver(size); ro.observe(el);
  let drag = null;
  cv.addEventListener("pointerdown", e => { drag = [e.clientX, e.clientY, yaw, pitch]; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener("pointerup", () => { drag = null; });
  cv.addEventListener("pointermove", e => {
    if (drag) { yaw = drag[2] - (e.clientX - drag[0]) * 0.006; pitch = Math.max(-1.2, Math.min(1.2, drag[3] + (e.clientY - drag[1]) * 0.005)); return; }
    hover(e);
  });
  cv.addEventListener("wheel", e => { e.preventDefault();
    const k = Math.exp(e.deltaY * 0.0015);
    if (e.shiftKey) dist = Math.max(4, Math.min(260, dist * k));
    else { focus = Math.max(0, Math.min(10.3 * DEC, focus + e.deltaY * 0.02)); dist = Math.max(8, Math.min(260, dist * Math.sqrt(k))); }
  }, { passive: false });
  const ray = new T.Raycaster(), mouse = new T.Vector2();
  function hover(e) {
    const r = cv.getBoundingClientRect(); mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, cam); let best = null, bd = 1e9;
    for (const sp of objs) { const p = sp.position.clone().project(cam); const d = Math.hypot((p.x - mouse.x) * r.width / 2, (p.y - mouse.y) * r.height / 2); if (d < 14 && d < bd && p.z < 1) { bd = d; best = sp; } }
    if (best) { const o = best.userData.o; tip.innerHTML = `<b>${esc(o.name)}</b><span>${esc(o.kind)} · ${fmtLyShort(best.userData.now || o.ly)} away</span>`;
      tip.style.left = (e.clientX - r.left + 14) + "px"; tip.style.top = (e.clientY - r.top + 10) + "px"; tip.classList.add("on"); }
    else tip.classList.remove("on");
  }

  function layout() {               // place everything for the current slider time
    let i = 0;
    for (const sp of objs) {
      const o = sp.userData.o; let x = o.x, y = o.y, ly = o.ly;
      if (time > 0 && o.cls === "star") {          // r(t) = r0 + v t, in light years
        const ve = o.v[0] * KMS_LY * time, vn = o.v[1] * KMS_LY * time, vr = o.v[2] * KMS_LY * time;
        const nly = ly + vr; x = o.x + (ve / ly) * 57.29578; y = o.y + (vn / ly) * 57.29578; ly = Math.max(0.05, Math.hypot(nly, ve, vn));
      }
      sp.userData.now = ly;
      const p0 = place(o.x, o.y, o.ly), p = place(x, y, ly);
      sp.position.copy(p);
      trailPos.set([p0.x, p0.y, p0.z, p.x, p.y, p.z], i * 6); i++;
      let a = 1;
      if (time < 0) { const ds = Math.abs(sOf(o.ly) - sOf(-time)); a = ds < 0.12 ? 1 : 0.28; sp.scale.setScalar(sp.userData.base * (ds < 0.12 ? 1.7 : 1)); }
      else sp.scale.setScalar(sp.userData.base);
      sp.material.opacity = a;
    }
    trailGeo.attributes.position.needsUpdate = true; trails.visible = time > 0;
    if (time < 0) { const s = sOf(-time) * DEC, w = Math.tan(HALF_ANGLE) * Math.max(s, .4) * 2; shell.position.set(0, 0, -s); shell.scale.set(w, w, 1); shellMat.opacity = .14; }
    else shellMat.opacity = 0;
    if (tgt) { const sp = objs.find(s => s.userData.o === tgt); ring.position.copy(sp.position); }
  }
  function drawRuler() {            // light years from Earth along the axis, wherever the camera is
    const g2 = ruler.getContext("2d"), d = ruler.width / 120, H = ruler.height;
    g2.clearRect(0, 0, ruler.width, H);
    g2.strokeStyle = "rgba(255,255,255,.35)"; g2.lineWidth = d; g2.beginPath(); g2.moveTo(28 * d, 0); g2.lineTo(28 * d, H); g2.stroke();
    g2.font = `${11 * d}px Montserrat, sans-serif`; g2.fillStyle = "rgba(255,255,255,.8)";
    const ticks = [];
    for (let k = 0; k <= 10; k++) for (const m of k < 10 ? [1, 2, 5] : [1]) ticks.push(m * 10 ** k);
    let lastY = -1e9;
    for (const ly of ticks) {
      const p = new T.Vector3(0, 0, -sOf(ly) * DEC).project(cam); if (p.z > 1 || p.z < -1) continue;
      const y = (1 - (p.y + 1) / 2) * H; if (y < 0 || y > H) continue;
      const major = Math.log10(ly) % 1 === 0; if (!major && Math.abs(y - lastY) < 26 * d) continue;
      g2.beginPath(); g2.moveTo((major ? 18 : 23) * d, y); g2.lineTo(28 * d, y); g2.stroke();
      if (Math.abs(y - lastY) >= 18 * d) { g2.fillText(fmtLyShort(ly), 34 * d, y + 4 * d); lastY = y; }
    }
    // where the camera is looking
    const fy = (1 - (new T.Vector3(0, 0, -focus).project(cam).y + 1) / 2) * H;
    g2.fillStyle = "#e8be8c"; g2.beginPath(); g2.moveTo(28 * d, fy); g2.lineTo(20 * d, fy - 5 * d); g2.lineTo(20 * d, fy + 5 * d); g2.fill();
  }
  let raf = 0;
  const loop = () => {
    const fz = -focus;
    cam.position.set(Math.sin(yaw) * Math.cos(pitch) * dist, Math.sin(pitch) * dist, fz + Math.cos(yaw) * Math.cos(pitch) * dist);
    cam.lookAt(0, 0, fz);
    layout(); renderer.render(scene, cam); drawRuler();
    raf = requestAnimationFrame(loop);
  };
  loop();
  st = { renderer, ro, raf: () => raf, setTime: t => { time = t; }, data, tgt };
  return st;
}

export function setTime(t) { if (st) st.setTime(t); }
export function close() {
  if (!st) return;
  cancelAnimationFrame(st.raf()); st.ro.disconnect(); st.renderer.dispose(); st = null;
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const sameName = (a, b) => { const x = norm(a), y = norm(b); return x && y && (x === y || x.includes(y) || y.includes(x)); };

// slider position (-1000..1000) <-> years (negative = past). Log scale both ways, "now" in the middle.
export function sliderYears(v) {
  if (Math.abs(v) < 8) return 0;
  const f = (Math.abs(v) - 8) / 992;
  return v < 0 ? -(10 ** (f * Math.log10(1.38e10))) : 10 ** (f * 5);
}
export function timeLabel(t, data, res) {
  if (t === 0) return "<b>Now</b> · drag left to look back in time, right to watch the stars move";
  const y = Math.abs(t), span = y < 1e3 ? `${Math.round(y).toLocaleString()} years` : y < 1e6 ? `${Math.round(y / 1e3).toLocaleString()} thousand years`
    : y < 1e9 ? `${(y / 1e6).toFixed(y < 1e7 ? 1 : 0)} million years` : `${(y / 1e9).toFixed(1)} billion years`;
  if (t < 0) {
    const objs = (data && data.objects) || [], hit = objs.filter(o => Math.abs(Math.log10(o.ly) - Math.log10(y)) < 0.12);
    return `<b>${span} ago</b> · light arriving now from the glowing slice left then` + (hit.length ? ` · ${hit.length} object${hit.length > 1 ? "s" : ""} in it` : "");
  }
  const o = st && st.tgt;
  if (o && o.cls === "star") {
    const ve = o.v[0] * KMS_LY * t, vn = o.v[1] * KMS_LY * t, vr = o.v[2] * KMS_LY * t, d = Math.hypot(o.ly + vr, ve, vn);
    return `<b>In ${span}</b> · ${esc(res.name)} will be ${fmtLyShort(d)} away (now ${fmtLyShort(o.ly)})`;
  }
  return `<b>In ${span}</b> · each star moves along its measured 3D velocity; galaxies barely change`;
}
