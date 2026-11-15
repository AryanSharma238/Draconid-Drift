// Smooth, north-up cutouts straight from HiPS tiles.
// Aladin draws deep zooms as hard diamond pixels, so for the close-up we resample the same tiles ourselves
// with bilinear filtering on the true HEALPix lattice. Also makes the search thumbnails.
const R = Math.PI / 180;
const spread = v => { let r = 0; for (let b = 0; b < 15; b++) r += ((v >> b) & 1) * 4 ** b; return r; };   // bit i -> bit 2i

// Continuous HEALPix coordinates: face, and fractional lattice position (fx, fy) in units of cells at `nside`.
function hpxContinuous(ra, dec, nside) {
  const z = Math.sin(dec * R), za = Math.abs(z);
  let tt = ((ra % 360) + 360) % 360 / 90;
  let face, fx, fy;
  if (za <= 2 / 3) {
    const t1 = nside * (0.5 + tt), t2 = nside * z * 0.75;
    const jp = t1 - t2, jm = t1 + t2;
    const ifp = Math.floor(jp / nside), ifm = Math.floor(jm / nside);
    face = ifp === ifm ? (ifp | 4) : (ifp < ifm ? ifp : ifm + 8);
    fx = jm - Math.floor(jm / nside) * nside;
    fy = nside - (jp - Math.floor(jp / nside) * nside);
  } else {
    const ntt = Math.min(Math.floor(tt), 3), tp = tt - ntt;
    const tmp = nside * Math.sqrt(3 * (1 - za));
    const jp = Math.min(tp * tmp, nside), jm = Math.min((1 - tp) * tmp, nside);
    if (z >= 0) { face = ntt; fx = nside - jm; fy = nside - jp; }
    else { face = ntt + 8; fx = jp; fy = jm; }
  }
  return { face, fx, fy };
}

const tileCache = new Map();
function loadTile(base, order, idx, slice = null, fmt = "jpg") {
  const key = base + order + "/" + idx + "_" + slice;
  if (tileCache.has(key)) return tileCache.get(key);
  const dir = Math.floor(idx / 10000) * 10000;
  const p = new Promise(res => {
    const im = new Image(); im.crossOrigin = "anonymous";
    im.onload = () => {
      const c = document.createElement("canvas"); c.width = im.naturalWidth; c.height = im.naturalHeight;
      const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(im, 0, 0);
      res({ w: c.width, h: c.height, d: g.getImageData(0, 0, c.width, c.height).data });
    };
    im.onerror = () => res(null);
    im.src = `${base}/Norder${order}/Dir${dir}/Npix${idx}${slice ? "_" + slice : ""}.${fmt}`;
  });
  tileCache.set(key, p);
  return p;
}

// Which tiles does a patch touch? (sampled on a coarse grid)
function tilesFor(ra, dec, fovDeg, order, N = 9) {
  const set = new Set(), nsT = 2 ** order, nsC = nsT * 512;
  for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) {
    const [r, d] = tanToSky(ra, dec, ((i / (N - 1)) - 0.5) * fovDeg * R * 1.1, ((j / (N - 1)) - 0.5) * fovDeg * R * 1.1);
    const c = hpxContinuous(r, d, nsC);
    set.add(tileIndex(c, Math.floor(c.fx), Math.floor(c.fy), order));
  }
  return [...set];
}
function tileIndex(c, ix, iy, order) {
  return c.face * 4 ** order + spread(ix >> 9) + 2 * spread(iy >> 9);
}

// gnomonic: tangent-plane offsets xi (east), eta (north), radians -> sky
function tanToSky(ra0, dec0, xi, eta) {
  const a0 = ra0 * R, d0 = dec0 * R, rho = Math.hypot(xi, eta);
  if (rho < 1e-12) return [ra0, dec0];
  const c = Math.atan(rho), sc = Math.sin(c), cc = Math.cos(c);
  const dec = Math.asin(cc * Math.sin(d0) + eta * sc * Math.cos(d0) / rho);
  const ra = a0 + Math.atan2(xi * sc, rho * Math.cos(d0) * cc - eta * Math.sin(d0) * sc);
  return [((ra / R) % 360 + 360) % 360, dec / R];
}

/**
 * Render a north-up (east-left) gnomonic image of the sky.
 * survey: {url, order}. fovDeg is the width of the image. Returns a canvas.
 * `tweak` lets the caller adjust colour per pixel batch (gamma/brightness) -> {gain, gamma}
 */
export async function renderCutout(survey, ra, dec, fovDeg, w, h, tweak = {}, opts = {}) {
  const order = survey.order, nsC = 2 ** order * 512;
  const tiles = new Map();
  await Promise.all(tilesFor(ra, dec, fovDeg * Math.hypot(1, h / w), order, 11).map(async i => tiles.set(i, await loadTile(survey.url, order, i, survey.slice, survey.fmt || "jpg"))));
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const g = cv.getContext("2d"), out = g.createImageData(w, h), od = out.data;
  const scale = fovDeg * R / w, gain = tweak.gain ?? 1, gam = tweak.gamma ?? 1, black = tweak.black ?? 0, mono = !!tweak.mono;
  const orient = opts.orient ?? 3;
  const px = (face, ix, iy) => {
    const n = nsC; ix = Math.min(Math.max(ix, 0), n - 1); iy = Math.min(Math.max(iy, 0), n - 1);
    const t = tiles.get(face * 4 ** order + spread(ix >> 9) + 2 * spread(iy >> 9));
    if (!t) return null;
    let x = ix & 511, y = iy & 511;
    if (orient & 2) { const q = x; x = y; y = q; }
    if (orient & 4) x = 511 - x;
    const row = (orient & 1) ? y : 511 - y, o4 = (row * 512 + x) * 4;
    return t.d.subarray(o4, o4 + 4);
  };
  let t0 = performance.now();
  for (let j = 0; j < h; j++) {
  for (let i = 0; i < w; i++) {
    const xi = -(i + 0.5 - w / 2) * scale, eta = (h / 2 - (j + 0.5)) * scale;
    const [r, d] = tanToSky(ra, dec, xi, eta);
    const c = hpxContinuous(r, d, nsC);
    const gx = c.fx - 0.5, gy = c.fy - 0.5, x0 = Math.floor(gx), y0 = Math.floor(gy), wx = gx - x0, wy = gy - y0;
    const a = px(c.face, x0, y0), b = px(c.face, x0 + 1, y0), cc = px(c.face, x0, y0 + 1), dd = px(c.face, x0 + 1, y0 + 1);
    const o = (j * w + i) * 4;
    if (!a || !b || !cc || !dd) { od[o] = 19; od[o + 1] = 22; od[o + 2] = 26; od[o + 3] = 255; continue; }
    if (mono) {          // one grey scale, light on the page colour (19,22,26) -> (238,240,244)
      let v = 0;
      for (let k = 0; k < 3; k++) v += (a[k] * (1 - wx) + b[k] * wx) * (1 - wy) + (cc[k] * (1 - wx) + dd[k] * wx) * wy;
      v = (v / 765 - black) / (1 - black); if (v < 0) v = 0;
      v = Math.pow(v * gain, gam); if (v > 1) v = 1;
      od[o] = 19 + v * 219; od[o + 1] = 22 + v * 218; od[o + 2] = 26 + v * 218;
    } else for (let k = 0; k < 3; k++) {
      let v = (a[k] * (1 - wx) + b[k] * wx) * (1 - wy) + (cc[k] * (1 - wx) + dd[k] * wx) * wy;
      v = (v / 255 - black) / (1 - black); if (v < 0) v = 0;
      v = Math.pow(v * gain, gam); od[o + k] = v > 1 ? 255 : v * 255;
    }
    od[o + 3] = 255;
  }
  if ((j & 7) === 7 && performance.now() - t0 > 6) { await new Promise(r => setTimeout(r, 0)); t0 = performance.now(); }
  }
  g.putImageData(out, 0, 0);
  return cv;
}
export const SURVEYS = {
  wise: { url: "https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/RGB-W4-W2-W1", order: 8 },
  tmass: { url: "https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color", order: 9 },
  w1: { url: "https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W1", order: 8 },
  w2: { url: "https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W2", order: 8 },
  spherex: slice => ({ url: "https://irsa.ipac.caltech.edu/data/hips/SPHEREx/spherex_allsky_cube", order: 5, slice, fmt: "png" }),
  dss:  { url: "https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/color", order: 9 },
};
