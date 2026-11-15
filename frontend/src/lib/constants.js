import { SURVEYS } from "./healpix.js";
import { unit } from "./sky.js";

export const MAS = 3.6e6;
export const PX = 6.15;
export const FRAME_FACTOR = 3.0;
export const WISETONE = { mono: true, black: 0, gain: 1, gamma: 1 };
export const SPX_PSF = 9.0;
export const WISE_PSF = { W1: 6.1, W2: 6.4, J: 2.9, H: 2.8, K: 2.9 };

export const KINDS = {
  barnard: "Red dwarf, fast mover",
  epsindi: "Brown dwarf pair",
  wise0855: "Coldest known brown dwarf",
  rwaur: "Young variable star",
  rdra: "Mira variable star",
};

export const OTYPE = {
  "*": "Star",
  "**": "Double star",
  "BY*": "Star",
  "Mi*": "Mira variable",
  "BD*": "Brown dwarf",
  "Y*O": "Young star",
  "Er*": "Flare star",
  QSO: "Quasar",
  G: "Galaxy",
  AGN: "Active galaxy",
  PN: "Nebula",
  HII: "Nebula",
  SNR: "Supernova remnant",
  Psr: "Pulsar",
  HXB: "X-ray binary",
  "V*": "Variable star",
  "Pu*": "Pulsating star",
  "Ro*": "Rotating star",
  "WD*": "White dwarf",
  "RGB*": "Giant star",
  "LM*": "Low-mass star",
  "Cl*": "Star cluster",
  GlC: "Globular cluster",
};

export const FAMOUS = [
  ["Sirius", 101.287155, -16.716116, -546.01, -1223.07, "Brightest star"],
  ["Polaris", 37.954561, 89.264109, 44.48, -11.85, "North Star"],
  ["Alpha Centauri", 219.902066, -60.833975, -3679.25, 473.67, "Nearest star system"],
  ["Proxima Centauri", 217.428953, -62.679484, -3781.74, 769.47, "Nearest star"],
  ["TON 618", 187.104025, 31.477119, 0, 0, "Quasar"],
  ["Betelgeuse", 88.792939, 7.407064, 27.54, 11.3, "Red supergiant"],
  ["Vega", 279.234735, 38.783689, 200.94, 286.23, "Star"],
  ["Rigel", 78.634467, -8.201638, 1.31, 0.5, "Blue supergiant"],
  ["Arcturus", 213.9153, 19.182409, -1093.39, -2000.06, "Red giant"],
  ["Andromeda Galaxy", 10.684708, 41.26875, 0, 0, "Galaxy", 190],
  ["Orion Nebula", 83.822083, -5.391111, 0, 0, "Nebula", 66],
  ["Sagittarius A*", 266.416837, -29.00781, 0, 0, "Galactic centre"],
  ["Eta Carinae", 161.264775, -59.684431, -7.6, 1.0, "Massive star"],
  ["Crab Nebula", 83.633083, 22.0145, 0, 0, "Supernova remnant", 7],
  ["3C 273", 187.277915, 2.052388, 0, 0, "Quasar"],
  ["Pleiades", 56.75, 24.116667, 0, 0, "Star cluster", 110],
  ["Kapteyn's Star", 77.919, -45.018, 6505, -5731, "Fast-moving star"],
  ["61 Cygni", 316.725, 38.749, 4164, 3250, "Fast-moving pair"],
  ["Groombridge 1830", 178.245, 37.719, 4003, -5813, "Fast-moving star"],
  ["Wolf 359", 164.12, 7.015, -3842, -2725, "Nearby red dwarf"],
].map(([name, ra, dec, pmra, pmdec, kind, size]) => ({
  key: "custom",
  name,
  ra,
  dec,
  pmra,
  pmdec,
  epoch: 2000,
  kind,
  size: size || 0,
}));

export const smooth = (x) => x * x * (3 - 2 * x);
export const clamp01 = (x) => Math.max(0, Math.min(1, x));

export const detNum = (d) => +String(d || "SPHEREx-D3").slice(-1) || 3;

export const compareFor = (d) =>
  detNum(d) <= 3
    ? { epoch: 2000.0, survey: [null, SURVEYS.tj, SURVEYS.th, SURVEYS.tk][detNum(d)] }
    : { epoch: 2010.5, survey: detNum(d) === 4 ? SURVEYS.w1 : SURVEYS.w2 };

export function propagate(it, year) {
  const dt = year - (it.epoch ?? 2000);
  const dec1 = it.dec + ((it.pmdec || 0) * dt) / MAS;
  const ra1 =
    it.ra +
    ((it.pmra || 0) * dt) / MAS / Math.cos((((it.dec + dec1) / 2) * Math.PI) / 180);
  return [ra1, dec1];
}

export const fieldDeg = (fov) => {
  let n = Math.ceil(fov / PX);
  if (n % 2 === 0) n += 1;
  return (n * PX) / 3600;
};

export function sexa(ra, dec) {
  const h = ra / 15,
    H = Math.floor(h),
    M = Math.floor((h - H) * 60),
    S = ((h - H) * 60 - M) * 60;
  const a = Math.abs(dec),
    D = Math.floor(a),
    m = Math.floor((a - D) * 60),
    s = Math.round(((a - D) * 60 - m) * 60);
  return [
    `RA   ${H}h ${String(M).padStart(2, "0")}m ${S.toFixed(1).padStart(4, "0")}s`,
    `Dec ${dec < 0 ? "−" : "+"}${String(D).padStart(2, "0")}° ${String(m).padStart(2, "0")}′ ${String(s).padStart(2, "0")}″`,
  ];
}

export const sepDeg = (a, b) => {
  const u = unit(a[0], a[1]),
    v = unit(b[0], b[1]);
  return (
    (Math.acos(Math.max(-1, Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))) *
      180) /
    Math.PI
  );
};

export function offsetOf(c, t) {
  const R = Math.PI / 180,
    a0 = c[0] * R,
    d0 = c[1] * R,
    a = t[0] * R,
    d = t[1] * R;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a - a0);
  const xi = (Math.cos(d) * Math.sin(a - a0)) / cosc;
  const eta =
    (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a - a0)) / cosc;
  return cosc > 0 ? [-xi / R, eta / R] : [180, 0];
}

export function parseCoords(q, midEpoch) {
  const m = q.replace(/[,;]/g, " ").trim().split(/\s+/).map(Number);
  if (m.length === 2 && m.every(isFinite) && Math.abs(m[1]) <= 90) {
    return {
      key: "custom",
      name: `${m[0].toFixed(3)}°, ${m[1] >= 0 ? "+" : ""}${m[1].toFixed(3)}°`,
      ra: ((m[0] % 360) + 360) % 360,
      dec: m[1],
      kind: "Coordinates",
      epoch: midEpoch,
    };
  }
  return null;
}

export function parseSesame(t, q) {
  const J = t.match(/%J\s+([\d.]+)\s+([+-][\d.]+)/);
  const P2 = t.match(/%P\s+([-\d.]+)\s+([-\d.]+)/);
  const N = t.match(/%I\.0\s+(.+)/);
  const C = t.match(/%C\.0\s+(\S+)/);
  return J
    ? [
        {
          key: "custom",
          name: (N ? N[1] : q).trim().replace(/^NAME\s+/, ""),
          ra: +J[1],
          dec: +J[2],
          pmra: P2 ? +P2[1] : 0,
          pmdec: P2 ? +P2[2] : 0,
          epoch: 2000,
          kind: C ? OTYPE[C[1]] || C[1] : "",
        },
      ]
    : [];
}

export function toPx(res, ra, dec, cw, ch) {
  const s = res.field_deg * 3600,
    cd = Math.cos((res.dec_c * Math.PI) / 180);
  let dra = ra - res.ra_c;
  dra = ((dra + 540) % 360) - 180;
  return [
    cw / 2 - (ch * dra * cd * 3600) / s,
    ch / 2 - (ch * (dec - res.dec_c) * 3600) / s,
  ];
}

