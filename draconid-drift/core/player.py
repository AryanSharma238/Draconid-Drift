"""Smooth flipbook player.

Why not Plotly: Plotly animations with images must use redraw=True, which re-renders the
whole figure every frame and flickers. Instead we:
  1. build motion-compensated in-between frames on the server with classic (non-ML)
     bidirectional optical flow (OpenCV Farneback + cv2.remap), and
  2. play them in a <canvas> driven by requestAnimationFrame, with every frame preloaded,
     sub-frame crossfading and eased timing, so playback is locked to the display refresh.
"""
from __future__ import annotations

import base64
import io
import json

import numpy as np


# ── 1. Classic optical-flow frame interpolation ───────────────────────────────
def _gray(rgb01: np.ndarray) -> np.ndarray:
    g = (0.299 * rgb01[..., 0] + 0.587 * rgb01[..., 1] + 0.114 * rgb01[..., 2])
    return (np.clip(g, 0, 1) * 255).astype("uint8")


def _flow(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.calcOpticalFlowFarneback(_gray(a), _gray(b), None, pyr_scale=0.5, levels=4, winsize=21,
                                        iterations=5, poly_n=7, poly_sigma=1.5, flags=0)


def _warp(img: np.ndarray, flow: np.ndarray) -> np.ndarray:
    """Backward warp: out(x) = img(x + flow(x))."""
    import cv2
    h, w = flow.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    mx = (gx + flow[..., 0]).astype(np.float32); my = (gy + flow[..., 1]).astype(np.float32)
    return cv2.remap(img.astype(np.float32), mx, my, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REFLECT)


def inbetweens(a: np.ndarray, b: np.ndarray, n: int, max_flow_px: float = 6.0) -> list[np.ndarray]:
    """n motion-compensated frames strictly between RGB frames a and b (0..1 floats).

    Bidirectional flow is combined into intermediate flows with the standard linear
    approximation  F(t→0) = -(1-t)t·F01 + t²·F10,  F(t→1) = (1-t)²·F01 - t(1-t)·F10,
    both source frames are warped to time t and blended with weights (1-t, t).
    Flow is clamped so noise can never drag pixels far; where flow is unreliable the
    result degrades gracefully to a plain crossfade.
    """
    try:
        f01, f10 = _flow(a, b), _flow(b, a)
    except Exception:
        return [(1 - t) * a + t * b for t in np.linspace(0, 1, n + 2)[1:-1]]
    for f in (f01, f10):
        mag = np.linalg.norm(f, axis=-1, keepdims=True)
        f *= np.minimum(1.0, max_flow_px / np.maximum(mag, 1e-6))
    out = []
    for t in np.linspace(0, 1, n + 2)[1:-1]:
        ft0 = -(1 - t) * t * f01 + t * t * f10
        ft1 = (1 - t) ** 2 * f01 - t * (1 - t) * f10
        w0, w1 = _warp(a, ft0), _warp(b, ft1)
        out.append(np.clip((1 - t) * w0 + t * w1, 0, 1))
    return out


# ── 2. Canvas player ─────────────────────────────────────────────────────────
def _jpeg_uri(rgb01: np.ndarray, quality: int = 92) -> str:
    from PIL import Image
    a = (np.flipud(np.clip(np.nan_to_num(rgb01), 0, 1)) * 255).astype("uint8")  # north up
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="JPEG", quality=quality, subsampling=0)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def build_frames(visits: list[np.ndarray], n_between: int = 10, flow: bool = True):
    """Return (real, (frames, keys)): real visits only, and the smooth sequence with the index
    of each real visit inside it."""
    real = [_jpeg_uri(v) for v in visits]
    frames, keys = [], []
    for i, v in enumerate(visits):
        keys.append(len(frames))
        frames.append(real[i])
        if i < len(visits) - 1:
            mids = inbetweens(v, visits[i + 1], n_between) if flow else \
                [(1 - t) * v + t * visits[i + 1] for t in np.linspace(0, 1, n_between + 2)[1:-1]]
            frames += [_jpeg_uri(m) for m in mids]
    return real, (frames, keys)


def html(real: list[str], tween: tuple[list[str], list[int]], labels: list[str], markers: dict | None = None,
         height: int = 640, hold_ms: int = 1100, travel_ms: int = 1500, note: str = "") -> str:
    """Self-contained canvas player.
    Default: ONLY the real SPHEREx visits, one after another (no generated frames).
    The "In-betweens" button switches to the smooth sequence `tween` = (frames, keys).
    markers: {'target': [[x,y] per visit], 'others': [...]} in fractional image coords (origin bottom-left)."""
    cfg = dict(real=real, tframes=tween[0], tkeys=tween[1], labels=labels, markers=markers or {},
               hold=hold_ms, travel=travel_ms)
    return f"""
<div id="pl" style="font-family:'DM Sans',-apple-system,Helvetica Neue,sans-serif;background:#0d1215;
  border-radius:22px;padding:20px 20px 16px;color:#f4f3f1;user-select:none">
  <div style="position:relative;width:100%;height:{height - 110}px;display:flex;justify-content:center">
    <canvas id="cv" style="height:100%;aspect-ratio:1/1;max-width:100%;border-radius:10px;background:#000"></canvas>
    <div id="ld" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
      color:#9aa0a6;font-size:14px">Preparing frames…</div>
  </div>
  <div style="display:flex;align-items:center;gap:14px;margin-top:16px">
    <button id="pp" style="all:unset;cursor:pointer;background:#f4f3f1;color:#0d1215;border-radius:999px;
      padding:9px 18px;font-size:14px;font-weight:500;min-width:58px;text-align:center">Pause</button>
    <div style="flex:1">
      <div id="lb" style="font-size:15px;margin-bottom:8px;min-height:20px"></div>
      <div id="tr" style="position:relative;height:6px;border-radius:99px;background:#3a3f42;cursor:pointer">
        <div id="fl" style="position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:#f4f3f1;width:0"></div>
        <div id="tk"></div>
      </div>
    </div>
    <button id="ib" style="all:unset;cursor:pointer;border:1.5px solid #f4f3f1;color:#f4f3f1;border-radius:999px;
      padding:8px 16px;font-size:13px;white-space:nowrap">In-betweens: off</button>
    <select id="sp" style="background:#1c2226;color:#f4f3f1;border:1px solid #3a3f42;border-radius:999px;
      padding:7px 12px;font-family:inherit;font-size:13px">
      <option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option>
    </select>
  </div>
  <div id="nt" style="font-size:12px;color:#9aa0a6;margin-top:10px">Showing only real SPHEREx visits.</div>
</div>
<script>
const C = {json.dumps(cfg)};
const C_NOTE_R = "Showing only real SPHEREx visits.";
const C_NOTE_T = {json.dumps(note)};
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const load = a => a.map(s => {{ const i = new Image(); i.decoding = 'async'; i.src = s; return i; }});
const REAL = load(C.real), TWEEN = load(C.tframes);
let tweenOn = false, imgs = REAL, K = REAL.map((_, i) => i), N = imgs.length;
const V = C.real.length;
let playing = true, speed = 1, clock = 0, last = null;
const travel = () => tweenOn ? C.travel : 0;             // real-only: clean cut, no generated frames
const seg = () => (C.hold + travel()) / speed;          // time per visit
const total = () => V > 1 ? seg() * V : 1;              // loops back to the first visit
const ease = t => t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;   // easeInOutCubic

// position along the frame list (float), plus visit progress for markers
function state(ms) {{
  if (V < 2) return {{f: 0, v: 0, u: 0}};
  const s = seg(), i = Math.floor(ms / s) % V, local = ms - Math.floor(ms / s) * s;
  const h = C.hold / speed;
  if (local < h || !tweenOn) return {{f: K[i], v: i, u: 0}};
  const u = ease((local - h) / (s - h));
  const j = (i + 1) % V;
  if (j === 0) return {{f: K[i], v: i, u, wrap: true}};     // last→first: crossfade only
  return {{f: K[i] + u * (K[j] - K[i]), v: i, u}};
}}

function ring(x, y, r, color, w) {{
  ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.strokeStyle = color; ctx.lineWidth = w; ctx.stroke();
}}
function lerpPt(a, b, u) {{ return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u]; }}

function draw(st) {{
  const W = cv.width, H = cv.height;
  ctx.globalAlpha = 1; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  if (st.wrap) {{                                          // smooth loop: dissolve last → first
    ctx.drawImage(imgs[K[V - 1]], 0, 0, W, H);
    ctx.globalAlpha = st.u; ctx.drawImage(imgs[0], 0, 0, W, H); ctx.globalAlpha = 1;
  }} else {{                                               // sub-frame crossfade between neighbours
    const a = Math.floor(st.f), b = Math.min(a + 1, N - 1), t = st.f - a;
    ctx.drawImage(imgs[a], 0, 0, W, H);
    if (t > 0.001) {{ ctx.globalAlpha = t; ctx.drawImage(imgs[b], 0, 0, W, H); ctx.globalAlpha = 1; }}
  }}
  const M = C.markers, j = (st.v + 1) % V;
  const toPx = p => [p[0] * W, (1 - p[1]) * H];
  if (M.others) for (const o of M.others) {{
    const p = toPx(V > 1 && !st.wrap ? lerpPt(o[st.v], o[j], st.u) : o[st.v]); ring(p[0], p[1], W * .022, 'rgba(126,214,160,.9)', 2);
  }}
  if (M.target) {{
    const p = toPx(V > 1 && !st.wrap ? lerpPt(M.target[st.v], M.target[j], st.u) : M.target[st.v]);
    ring(p[0], p[1], W * .034, '#e8a33d', 3);
  }}
  // UI
  const prog = V > 1 ? ((st.v + st.u) / V) : 0;
  document.getElementById('fl').style.width = (prog * 100) + '%';
  document.getElementById('lb').textContent = C.labels[st.u > .5 && !st.wrap ? j : st.v];
}}

function frame(ts) {{
  if (last === null) last = ts;
  if (playing) clock = (clock + (ts - last)) % total();
  last = ts; draw(state(clock)); requestAnimationFrame(frame);
}}

function resize() {{
  const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * d); cv.height = Math.round(r.height * d);
}}
// ticks for the real visits
const tk = document.getElementById('tk');
for (let i = 0; i < V; i++) {{
  const d = document.createElement('div');
  d.style.cssText = `position:absolute;top:-4px;width:14px;height:14px;border-radius:99px;background:#f4f3f1;
    border:3px solid #0d1215;left:calc(${{(i / Math.max(V, 1)) * 100}}% - 7px)`;
  tk.appendChild(d);
}}
document.getElementById('tr').onclick = e => {{
  const r = e.currentTarget.getBoundingClientRect(), p = (e.clientX - r.left) / r.width;
  const i = Math.min(V - 1, Math.round(p * V)); clock = i * seg();
}};
document.getElementById('pp').onclick = e => {{ playing = !playing; e.target.textContent = playing ? 'Pause' : 'Play'; }};
document.getElementById('ib').onclick = e => {{
  const v = clock / seg(); tweenOn = !tweenOn;
  imgs = tweenOn ? TWEEN : REAL; K = tweenOn ? C.tkeys : REAL.map((_, i) => i); N = imgs.length;
  clock = Math.floor(v) * seg();
  e.target.textContent = 'In-betweens: ' + (tweenOn ? 'on' : 'off');
  e.target.style.background = tweenOn ? '#f4f3f1' : 'transparent'; e.target.style.color = tweenOn ? '#0d1215' : '#f4f3f1';
  document.getElementById('nt').textContent = tweenOn ? C_NOTE_T : C_NOTE_R;
}};
document.getElementById('sp').onchange = e => {{ const p = clock / seg(); speed = +e.target.value; clock = p * seg(); }};
window.addEventListener('resize', resize);
Promise.all(REAL.concat(TWEEN).map(i => i.decode ? i.decode().catch(() => {{}}) : Promise.resolve())).then(() => {{
  document.getElementById('ld').remove(); resize(); requestAnimationFrame(frame);
}});
</script>"""
