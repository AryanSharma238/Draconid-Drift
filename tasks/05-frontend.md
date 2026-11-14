# Task 05 — Frontend MVP

**Model:** lower model OK. **Time:** ~1.5 days. **Depends on:** Task 04 (API running locally).

## Goal
A single-page blink viewer. The first frame is drawn the moment it arrives, playback runs at 60 fps with zero server calls, and the URL reproduces the view. Page weight < 100 KB.

## Constraints
- Vanilla ES modules (`<script type="module">`). No frameworks, no npm, no build, no CDN libraries in the MVP.
- Every module has one job (below). Pure functions are kept separate from DOM code so they can be tested.

## Files
```
frontend/index.html
frontend/style.css
frontend/js/app.js        # wiring: state, form, URL sync, calls other modules
frontend/js/state.js      # state object + URL <-> state (pure)
frontend/js/stream.js     # fetch + NDJSON parse (async generator)
frontend/js/decode.js     # base64 → Float32Array (pure)
frontend/js/stretch.js    # asinh/linear stretch, diff colormap (pure)
frontend/js/render.js     # draw frame to canvas, crosshair
frontend/js/player.js     # play/pause/step/speed using requestAnimationFrame
frontend/test/test.html   # in-browser tests for pure modules (open the file, see PASS/FAIL)
```

## Steps

### F1. `state.js` (pure)
```js
export const DEFAULTS = { size: 0.1, det: 6, mode: "pass", speed: 500, diff: "none", zoom: 0, embed: 0 };
export function fromURL(search) { /* URLSearchParams → state; parse numbers; unknown keys ignored; invalid → default */ }
export function toURL(state) { /* only non-default keys, fixed key order: ra,dec,q,size,det,mode,speed,minbright,maxbright,zoom,diff,embed */ }
export function parseCoords(text) { /* same rules as contract C3; returns {ra,dec} | null. Sexagesimal: split h m s / d m s, or ':' */ }
```
Required tests in `test.html`:
- `parseCoords("08h55m10.831s -07d14m42.53s")` → (133.79513, -7.24515) ± 1e-5.
- `parseCoords("133.795 -7.245")`.
- `parseCoords("-07d14m42.53s")` → null.
- `fromURL(toURL(s))` deep-equals `s`.

### F2. `stream.js`
```js
export async function* ndjson(url, signal) {
  const resp = await fetch(url, { signal });
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf);
}
```
A new query must abort the previous one (`AbortController`).

### F3. `decode.js` (pure)
```js
const LUT = (() => {                      // float16 bits → float32, built once (256 KB)
  const t = new Float32Array(65536);
  for (let h = 0; h < 65536; h++) {
    const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
    t[h] = e === 0 ? s * 2 ** -14 * (f / 1024)
         : e === 31 ? (f ? NaN : s * Infinity)
         : s * 2 ** (e - 15) * (1 + f / 1024);
  }
  return t;
})();
export function b64ToBytes(b64) { const s = atob(b64); const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; }
export function decodeF16(b64) { const u16 = new Uint16Array(b64ToBytes(b64).buffer); const out = new Float32Array(u16.length); for (let i = 0; i < u16.length; i++) out[i] = LUT[u16[i]]; return out; }
export function decodeU32(b64) { return new Uint32Array(b64ToBytes(b64).buffer); }
```
Test: build a known float16 base64 string in Python (`base64.b64encode(np.array([0,1,-2,np.nan,65504],'<f2').tobytes())`), paste it into test.html, and check the decoded values.

### F4. `stretch.js` (pure; must match contract C9 exactly)
```js
export function makeStretch(lo, hi, a = 0.1) {
  const k = 1 / Math.asinh(1 / a), span = hi - lo || 1;
  return x => { if (x !== x) return -1; let v = (x - lo) / span; v = v < 0 ? 0 : v > 1 ? 1 : v; return Math.round(255 * k * Math.asinh(v / a)); };
}
export function robustSigma(arr) { /* 1.4826 * median(|x - median|) over finite values; use a copy + sort, subsample to 20k */ }
export function diverging(x, lim) { /* returns [r,g,b]: neg → blue (0,120,255), 0 → black, pos → orange (255,140,0); linear in |x|/lim, clamped */ }
```
Tests:
- `makeStretch(0,10)(0) === 0`, `(10) === 255`, `(NaN) === -1`, and monotonic over 0..10.
- Same input values give the same output regardless of call order (no hidden state).

### F5. `render.js`
```js
export class View {
  constructor(canvas) { /* ctx = canvas.getContext("2d"); this.img = null */ }
  setSize(npix) { /* canvas.width = canvas.height = npix; this.img = ctx.createImageData(npix, npix) */ }
  draw(frameData /* Float32Array */, stretchFn) {
    // for each pixel i: b = stretchFn(v); if b < 0 → (40,0,0); else gray (b,b,b); alpha 255
    // ctx.putImageData(this.img, 0, 0)
  }
  drawDiff(a, b, lim) { /* a - b per pixel → diverging */ }
}
```
- The crosshair is a separate absolutely positioned SVG over the canvas (so it doesn't redraw with frames): two short lines with a gap at the centre, so it doesn't hide the target.
- CSS: `canvas { image-rendering: pixelated; width: calc(var(--npix) * var(--zoom) * 1px); }`. Auto zoom = `floor(min(viewportW, viewportH*0.7) / npix)`.
- Orientation: draw as received (contract C4). Show labels "N" (top) and "E" (left).

### F6. `player.js`
```js
export class Player {
  constructor(onShow /* (index) => void */) {}
  setCount(n) {} play() {} pause() {} toggle() {} step(d /* ±1 */) {} setSpeed(ms) {}
  // rAF loop: advance when (now - last) >= speedMs; wrap around; skip missing frames (not yet arrived)
}
```
Keyboard: Space = toggle, ←/→ = step, +/− = zoom. Ignore keys while focus is in an input.

### F7. `app.js` (wiring)
1. On load: `state = fromURL(location.search)`. If `q` is set, call `/api/resolve?q=` to get ra/dec. If ra/dec are set, call `load()`.
2. Form: one text input "Object name or coordinates", plus a size select (labels like `0.1° (~0.3 MB)`: estimate npix² × 2 B × expected frames), a detector select (D1–D6 with λ ranges [from recon]), and a mode toggle (All frames / Per-pass co-add). On submit: `parseCoords`, or else resolve as a name.
3. `load()`:
   - Abort the previous stream. Reset `frames = []`.
   - `for await (msg of ndjson(url))`:
     - `header` → `view.setSize(npix)`, store zscale, show "0 / n_expected".
     - `frame` → `frames[msg.i] = {meta, data: decodeF16(msg.data)}`. **If it's the first frame, draw it immediately.** Update progress.
     - `zscale` → update the stretch (unless the user overrode it) and redraw the current frame.
     - `error` → `failed++`, show the count.
     - `done` → hide progress, start playback.
4. Frame label under the canvas: `date (UTC) · D{det} · λ = {lambda_um.toFixed(3)} µm · pass {pass} · {i+1}/{n}`.
5. Stretch controls: two sliders (min/max brightness, initialised to zscale) and a "reset" button. Changing a slider redraws the current frame only. URL keys: minbright/maxbright.
6. Diff select: none | "− median of all" | "− first frame". The median frame is computed once per load (per-pixel median over loaded frames, ignoring NaN) and cached.
7. URL sync: `history.replaceState(null, "", "?" + toURL(state))` on every state change (debounce 200 ms).
8. Error states, shown in a banner:
   - Network error → "Can't reach server".
   - `n_expected == 0` → "No SPHEREx coverage here yet".
   - failed > 0 → "N frames failed to load", with the names shown on hover.
9. Embed mode (`embed=1`): hide the form and sliders; show only the canvas, play/pause and an "Open in full tool ↗" link (same URL without embed). Add `class="embed"` to `<body>` and handle it in CSS.

### F8. Performance checks
- Chrome DevTools Performance: play a 0.5° stack for 5 s → no frame > 16 ms in `draw`. If it fails, precompute a 65536-entry stretch LUT per stretch change (index by float16 bits; keep the raw Uint16 alongside the Float32) instead of calling asinh per pixel.
- Network panel: total transfer for index.html + css + js < 100 KB.

### F9. Visual checks on all 5 targets (record in `notes/frontend_check.md` with screenshots)

| target | det | mode | what to verify |
|---|---|---|---|
| WISE 0855 | 6 | pass | Source shifts between passes; crosshair near it |
| Barnard's Star | 3 & 6 | pass | Shift visible; saturation (NaN red) handled |
| Eps Ind B | 6 | pass | Shift visible (small) |
| RW Aur | 3 | all | Brightness changes, no flicker in field stars |
| R Dra | 3 | all | Large brightness change |

## Done when
- `frontend/test/test.html` opened in a browser shows all PASS. Paste the list.
- The F8 numbers are recorded.
- F9 is filled in, with screenshots in `notes/`.
- Loading `/?ra=133.795&dec=-7.245&size=0.1&det=6&mode=pass` from a fresh tab reproduces the view.

## Do NOT
- Add any library or framework.
- Make server calls during playback, stretch changes or diff changes.
- Compute stretch limits per frame.
