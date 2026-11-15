// The sky: one Aladin Lite instance driven by hand, so every animation frame is under our control.
const ALADIN_URL = "https://cdn.jsdelivr.net/npm/aladin-lite@3.8.2/dist/aladin.js";
let _A = null;
async function loadAladin() {
  if (!_A) {
    const mod = await import(/* @vite-ignore */ ALADIN_URL);
    _A = mod.default || mod;
  }
  return _A;
}

export const MAP_URL = "https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color";   // sharp 2MASS colour map for browsing and the flight
const R = Math.PI / 180;

export const unit = (ra, dec) => [Math.cos(dec * R) * Math.cos(ra * R), Math.cos(dec * R) * Math.sin(ra * R), Math.sin(dec * R)];
export const radec = v => {
  const dec = Math.asin(Math.max(-1, Math.min(1, v[2]))) / R;
  let ra = Math.atan2(v[1], v[0]) / R; if (ra < 0) ra += 360;
  return [ra, dec];
};
const dot = (u, v) => u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
export function slerp(u, v, t) {
  const om = Math.acos(Math.max(-1, Math.min(1, dot(u, v))));
  if (om < 1e-9) return v;
  const s = Math.sin(om), a = Math.sin((1 - t) * om) / s, b = Math.sin(t * om) / s;
  return [a * u[0] + b * v[0], a * u[1] + b * v[1], a * u[2] + b * v[2]];
}

export class Sky {
  constructor(el) { this.el = el; this.a = null; this.spin = { on: false, v: 0, vt: 0, raw: 0, id: 0 }; this.clock = null; }
  async init() {
    const A = await loadAladin();
    await A.init;
    const off = {};
    ["Fullscreen","Zoom","Layers","Goto","SimbadPointer","CooGrid","Settings","Share","Frame","Projection","StatusBar",
 "Reticle","Catalog","CooLocation","Fov","Credits","Logo"].forEach(k => { off["show" + k] = false; });
    ["Fullscreen","Zoom","Layers","Goto","SimbadPointer","CooGrid","Settings","Share","Projection"].forEach(k => { off["show" + k + "Control"] = false; });
    this.a = A.aladin(this.el, Object.assign({ survey: MAP_URL, fov: 360, target: "0 0", projection: "AIT", cooFrame: "ICRSd", showCooGrid: false }, off));
    this.el.querySelectorAll(".aladin-logo-container").forEach(n => n.remove());
    for (let i = 0; i < 200 && !this.a.getBaseImageLayer(); i++) await new Promise(r => setTimeout(r, 25));
    const h = this.a.getBaseImageLayer();
    try { this.a.setCooGrid({ color: "rgb(255,255,255)", opacity: 0.06, labelSize: 0, thickness: 1, enabled: false }); } catch (e) {}
    return this;
  }
  get W() { return this.el.clientWidth; }
  get H() { return this.el.clientHeight; }
  center() { const c = this.a.getRaDec(); return [c[0], c[1]]; }
  fov() { return this.a.getFoV()[0]; }
  set(ra, dec, fov) { this.a.setFoV(fov); this.a.gotoRaDec(ra, dec); }
  /** Zodiacal glow makes the infrared sky bright when zoomed in: black level = sky level on screen (raw 0-255 tile values). */
  setBlack(v) {}
  grid(on) { try { on ? this.a.showCooGrid() : this.a.hideCooGrid(); } catch (e) {} }
  pix2world(x, y) { try { const r = this.a.pix2world(x, y); return r && isFinite(r[0]) ? [r[0], r[1]] : null; } catch (e) { return null; } }
  /** FoV (deg across the viewport width) that shows the whole AIT ellipse inside the window, below the top bar. */
  homeFov(topPad = 90) {
    const W = this.W, H = this.H;
    return 360 * Math.max(1 / 0.93, W / (2 * (H - topPad - 16)));
  }
  startSpin(degPerSec = 2.2) {
    const s = this.spin; if (s.on) return;
    s.on = true; s.vt = degPerSec; s.raw = this.center()[0];
    let last = performance.now();
    const tick = now => {
      if (!s.on) return;
      const dt = Math.min(0.1, (now - last) / 1000); last = now;
      s.v += (s.vt - s.v) * (1 - Math.exp(-dt * 1.6));      // eases up from rest, no lurch
      s.raw = (s.raw - s.v * dt + 360) % 360;
      this.a.gotoRaDec(s.raw, 0);
      s.id = requestAnimationFrame(tick);
    };
    s.id = requestAnimationFrame(tick);
  }
  haltSpin() { const s = this.spin; s.on = false; s.v = 0; cancelAnimationFrame(s.id); }
}
