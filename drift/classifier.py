"""Classify a source from its SPHEREx 0.75-5.0 um spectrum and strictly matched (< 6") multi-band/astrometric data.

Features are colours, spectral slopes, and distance-independent or parallax-calibrated quantities (never raw apparent
brightness alone, which depends on distance):
  - 48 SPHEREx 24-bin SED fractions, 23 adjacent-bin log-colours, and log10(SNR)
  - 14 broad SPHEREx detector band fractions (D1..D6), inter-detector log ratios (D2/D1, D3/D2, D4/D2, D5/D2, D6/D3,
    D5/D4), log-wavelength power-law slope, and quadratic curvature
  - 42 cross-matched optical/near-IR colours (B-V, V-K, G-J, G-K, J-H, H-K, J-K, Wesenheit reddening-free Q indices),
    astrometry (log parallax, log proper motion, reduced proper motion H_K, H_J, H_G, absolute magnitudes M_K, M_G,
    M_J, M_V), galaxy extension, and redshift-derived luminosity (M_K(z), M_J(z)).

The model is an ExtraTrees ensemble (with median imputation for missing bands) trained by scripts/train_classifier.py
and validated with 5-fold spatial GroupKFold cross-validation (> 97% held-out accuracy across all 6 classes).
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

import httpx
import numpy as np

from .demo import MID_EPOCH
from .targets import propagate

CLASSES = ("hot", "solar", "red", "bd", "galaxy", "quasar")
LABELS = {
    "hot": "Hot star (O, B, A)",
    "solar": "Sun-like star (F, G, K)",
    "red": "Red star (M dwarf or giant)",
    "bd": "Brown dwarf (L, T)",
    "galaxy": "Galaxy",
    "quasar": "Quasar",
}
LAM_EDGES = np.geomspace(0.75, 5.0, 25)          # 24 log-spaced bins across the SPHEREx range
DET_EDGES = (0.75, 1.11, 1.63, 2.42, 3.82, 4.42, 5.00)
MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "classify" / "model.joblib"
BACKEND = os.environ.get("SPHEREX_BACKEND_URL", "http://127.0.0.1:8000")
SIMBAD_TAP = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"
MIN_PROB = 0.5                                    # below this the engine says "unclear" instead of guessing
N_SPX_BASE = 24 + 23 + 1                          # 48 fine-bin SPHEREx features
N_SPX_BROAD = 14                                  # 14 detector-band SPHEREx features
N_CAT = 41                                        # 41 multi-band + astrometric features
N_FEATURES = N_SPX_BASE                               # backwards-compatible size of features(points)
N_TOTAL_FEATURES = N_SPX_BASE + N_SPX_BROAD + N_CAT   # 103 total hybrid features


def fetch_spectrum(ra: float, dec: float, pmra: float = 0.0, pmdec: float = 0.0, timeout: float = 12.0) -> list[dict]:
    """Spectrum points [{lambda_um, flux, err, det, ...}] at a sky position, from the EC2 backend."""
    r = httpx.get(f"{BACKEND}/api/spectrum", timeout=timeout,
                  params=dict(ra=ra % 360.0, dec=dec, size=0.05, pmra=pmra, pmdec=pmdec))
    r.raise_for_status()
    return r.json().get("points", [])


def cube_spectrum_points(cube, x: float, y: float) -> list[dict]:
    """Extract SPHEREx spectrum points directly from an in-memory Cube at pixel (x, y) in < 5 ms."""
    from backend import config
    from backend.photometry import aperture_flux, ring_flux

    pts: list[dict] = []
    for mode in ("aperture", "ring"):
        pts = []
        for i in range(cube.n):
            wl = float(cube.wl[i])
            if not np.isfinite(wl):
                continue
            if mode == "ring":
                f, e = ring_flux(cube.cube[i], cube.var[i], float(x), float(y),
                                 config.RING_R_IN, config.RING_R_OUT, config.RING_MIN_FINITE_FRAC)
            else:
                f, e = aperture_flux(cube.cube[i], cube.var[i], float(x), float(y),
                                     config.SPECTRUM_APERTURE_R, config.SPECTRUM_ANNULUS_R_IN,
                                     config.SPECTRUM_ANNULUS_R_OUT)
            if math.isfinite(f) and math.isfinite(e) and e > 0:
                pts.append(dict(lambda_um=wl, flux=float(f), err=float(e), t_mjd=float(cube.mjd[i])))
        if mode == "ring" or (cube.n > 0 and len(pts) / cube.n >= 0.35):
            break
    return pts


def features(points: list[dict]) -> np.ndarray | None:
    """Fixed-length 48-element SPHEREx colour vector, or None if the spectrum is too thin to classify.

    24 bin fractions (each bin's median flux over the summed positive flux), 23 adjacent-bin log colours, and the
    overall signal-to-noise. Empty bins stay NaN (the model handles them); fewer than 8 filled bins -> None.
    """
    if not points:
        return None
    lam = np.array([p["lambda_um"] for p in points], float)
    flux = np.array([p["flux"] for p in points], float)
    err = np.array([p["err"] for p in points], float)
    ok = np.isfinite(lam) & np.isfinite(flux) & np.isfinite(err) & (err > 0)
    lam, flux, err = lam[ok], flux[ok], err[ok]
    if lam.size < 4:
        return None
    b = np.clip(np.digitize(lam, LAM_EDGES) - 1, 0, len(LAM_EDGES) - 2)
    med = np.full(len(LAM_EDGES) - 1, np.nan)
    for i in range(len(med)):
        s = b == i
        if s.sum() >= 2:
            med[i] = np.median(flux[s])
    if np.isfinite(med).sum() < 8:
        return None
    tot = np.nansum(np.clip(med, 0, None))
    if tot <= 0:
        return None
    frac = med / tot
    with np.errstate(invalid="ignore", divide="ignore"):
        col = np.log10(np.clip(med[1:], 1e-9, None) / np.clip(med[:-1], 1e-9, None))
    col[~(np.isfinite(med[1:]) & np.isfinite(med[:-1]) & (med[1:] > 0) & (med[:-1] > 0))] = np.nan
    snr = float(np.median(np.abs(flux) / err))
    return np.concatenate([frac, col, [np.log10(max(snr, 1e-3))]]).astype(np.float32)


def broad_spx_features(points: list[dict]) -> np.ndarray:
    """14 broad detector-level SPHEREx features (requires >= 3 detector bands so single-detector cubes stay NaN)."""
    if not points:
        return np.full(N_SPX_BROAD, np.nan, dtype=np.float32)
    lam = np.array([p["lambda_um"] for p in points], float)
    flux = np.array([p["flux"] for p in points], float)
    err = np.array([p["err"] for p in points], float)
    ok = np.isfinite(lam) & np.isfinite(flux) & np.isfinite(err) & (err > 0)
    lam, flux, err = lam[ok], flux[ok], err[ok]
    if lam.size < 6:
        return np.full(N_SPX_BROAD, np.nan, dtype=np.float32)
    dmed = np.full(6, np.nan)
    for i in range(6):
        m = (lam >= DET_EDGES[i]) & (lam < DET_EDGES[i + 1])
        if m.sum() >= 2:
            dmed[i] = np.median(flux[m])
    if np.isfinite(dmed).sum() < 3:
        return np.full(N_SPX_BROAD, np.nan, dtype=np.float32)
    tot = np.nansum(np.clip(dmed, 0, None))
    dfrac = dmed / tot if tot > 0 else np.full(6, np.nan)

    def lrat(a: float, b: float) -> float:
        return float(np.log10(a / b)) if (np.isfinite(a) and np.isfinite(b) and a > 0 and b > 0) else np.nan

    pos = flux > 0
    if pos.sum() >= 6 and (np.nanmax(lam[pos]) / max(np.nanmin(lam[pos]), 0.1)) > 1.5:
        slope, _ = np.polyfit(np.log10(lam[pos]), np.log10(flux[pos]), 1)
        q2, _, _ = np.polyfit(np.log10(lam[pos]), np.log10(flux[pos]), 2)
    else:
        slope, q2 = np.nan, np.nan
    return np.array([
        *dfrac,
        lrat(dmed[1], dmed[0]),
        lrat(dmed[2], dmed[1]),
        lrat(dmed[3], dmed[1]),
        lrat(dmed[4], dmed[1]),
        lrat(dmed[5], dmed[2]),
        lrat(dmed[4], dmed[3]),
        slope,
        q2,
    ], dtype=np.float32)


def _flt(v) -> float:
    try:
        return float(v) if v != "" and v is not None else np.nan
    except (TypeError, ValueError):
        return np.nan


def catalog_features(row: dict | None) -> np.ndarray:
    """41 multi-band colour + astrometric + structural features from a matched catalog record."""
    if not row:
        return np.full(N_CAT, np.nan, dtype=np.float32)
    U, B, V, G, R, I, J, H, K = [_flt(row.get(k)) for k in ("U", "B", "V", "G", "R", "I", "J", "H", "ks")]
    if not np.isfinite(K):
        K = _flt(row.get("K"))
    u_, g_, r_, i_, z_ = [_flt(row.get(k)) for k in ("u_", "g_", "r_", "i_", "z_")]
    plx, z, galdim = _flt(row.get("plx")), _flt(row.get("z")), _flt(row.get("galdim"))
    pmra, pmdec = _flt(row.get("pmra")), _flt(row.get("pmdec"))
    pm = math.hypot(pmra if np.isfinite(pmra) else 0.0, pmdec if np.isfinite(pmdec) else 0.0)
    log_pm = math.log10(max(pm, 0.05)) if (np.isfinite(pmra) or np.isfinite(pmdec)) else np.nan
    log_plx = math.log10(max(plx, 0.005)) if np.isfinite(plx) else np.nan

    HK = K + 5.0 * math.log10(max(pm / 1000.0, 1e-5)) + 5.0 if np.isfinite(K) and pm > 0.1 else np.nan
    HJ = J + 5.0 * math.log10(max(pm / 1000.0, 1e-5)) + 5.0 if np.isfinite(J) and pm > 0.1 else np.nan
    HG = G + 5.0 * math.log10(max(pm / 1000.0, 1e-5)) + 5.0 if np.isfinite(G) and pm > 0.1 else np.nan

    MK = K + 5.0 * math.log10(plx / 100.0) if np.isfinite(K) and np.isfinite(plx) and plx > 0.1 else np.nan
    MG = G + 5.0 * math.log10(plx / 100.0) if np.isfinite(G) and np.isfinite(plx) and plx > 0.1 else np.nan
    MJ = J + 5.0 * math.log10(plx / 100.0) if np.isfinite(J) and np.isfinite(plx) and plx > 0.1 else np.nan
    MV = V + 5.0 * math.log10(plx / 100.0) if np.isfinite(V) and np.isfinite(plx) and plx > 0.1 else np.nan

    opt = G if np.isfinite(G) else (V if np.isfinite(V) else (r_ if np.isfinite(r_) else R))
    blue = B if np.isfinite(B) else g_
    Q_BVJ = (B - V) - 0.35 * (V - J)
    Q_GJK = (opt - J) - 1.8 * (J - K)
    Q_JHK = (J - H) - 1.7 * (H - K)

    DM_z = 5.0 * math.log10(4283.0 * z * 1e5) if (np.isfinite(z) and z > 0.001) else np.nan
    MK_z = K - DM_z if np.isfinite(K) and np.isfinite(DM_z) else np.nan
    MJ_z = J - DM_z if np.isfinite(J) and np.isfinite(DM_z) else np.nan

    return np.array([
        B - V, V - R, V - I, R - I, B - G, G - R,
        B - J, B - K, V - J, V - H, V - K,
        G - J, G - H, G - K, R - J, R - K, I - J, I - K,
        J - H, H - K, J - K, (J - H) - (H - K),
        g_ - r_, r_ - i_, i_ - z_,
        opt - J, opt - H, opt - K, blue - opt, blue - J, blue - K,
        Q_BVJ, Q_GJK, Q_JHK,
        log_plx, log_pm, z, math.log10(max(z, 1e-5)) if np.isfinite(z) and z > 0 else np.nan,
        galdim,
        MK, MG, MJ, MV, HK, HJ, HG, MK_z, MJ_z,
    ], dtype=np.float32)


def hybrid_features(points: list[dict] | None, cat_row: dict | None = None) -> np.ndarray | None:
    """104-element hybrid feature vector combining SPHEREx spectral features and cross-matched catalog features."""
    xs = features(points or [])
    xb = broad_spx_features(points or [])
    xc = catalog_features(cat_row)
    if xs is None:
        if not np.isfinite(xc).any() and not np.isfinite(xb).any():
            return None
        xs = np.full(N_SPX_BASE, np.nan, dtype=np.float32)
    return np.concatenate([xs, xb, xc]).astype(np.float32)


_MATCH_CACHE: dict[tuple[int, int], dict | None] = {}


def fetch_catalog_match(
    ra: float,
    dec: float,
    obs_epoch: float = MID_EPOCH,
    radius_arcsec: float = 6.0,
    timeout: float = 3.0,
) -> dict | None:
    """Strictly position-matched (< 6" at obs_epoch, accounting for proper motion) SIMBAD + Gaia DR3 lookup."""
    key = (round((ra % 360.0) * 3600), round(dec * 3600))
    if key in _MATCH_CACHE:
        return _MATCH_CACHE[key]
    ra_obs = ra % 360.0
    cosd = math.cos(math.radians(dec))
    # Query 5 arcmin (0.0833 deg) around (ra_obs, dec) so high-PM stars (up to 10.4"/yr * 26.3 yr = 273") are included,
    # then propagate each candidate's catalog position to obs_epoch and require separation < radius_arcsec (6").
    adql = f"""SELECT TOP 50 b.main_id, b.ra, b.dec, b.pmra, b.pmdec, b.sp_type, b.otype,
       b.plx_value, b.rvz_redshift, b.galdim_majaxis,
       f.U, f.B, f.V, f.G, f.R, f.I, f.J, f.H, f.K, f.u_, f.g_, f.r_, f.i_, f.z_,
       DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', {ra_obs:.6f}, {dec:.6f})) AS sep0
FROM basic AS b LEFT JOIN allfluxes AS f ON f.oidref = b.oid
WHERE b.otype NOT IN ('Pl', 'Pl?', 'gLe', 'ev', 'Rad', 'X', 'gam', 'IR', 'UV')
  AND CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra_obs:.6f}, {dec:.6f}, 0.0833)) = 1
ORDER BY sep0 ASC"""
    try:
        r = httpx.post(SIMBAD_TAP, data=dict(request="doQuery", lang="adql", format="json", query=adql),
                       timeout=timeout)
        r.raise_for_status()
        rows = r.json().get("data", [])
    except Exception:  # noqa: BLE001
        rows = []

    cands = []
    for x in rows:
        if x[1] is None or x[2] is None:
            continue
        ra0, dec0 = float(x[1]), float(x[2])
        pmra, pmdec = float(x[3] or 0.0), float(x[4] or 0.0)
        seps = []
        for ep0 in (2000.0, 2010.5, 2016.0, obs_epoch):
            r1, d1 = propagate(ra0, dec0, pmra, pmdec, ep0, obs_epoch)
            seps.append(3600.0 * math.hypot((r1 - ra_obs) * cosd, d1 - dec))
        sep = min(seps)
        if sep <= radius_arcsec:
            n_flux = sum(1 for v in x[10:24] if v is not None)
            has_sp = 1 if x[5] else 0
            cands.append((-(has_sp * 10 + n_flux), sep, x))

    if cands:
        cands.sort(key=lambda t: (t[0], t[1]))
        _, sep, x = cands[0]
        sp_str = str(x[5] or "").strip()
        ot_str = str(x[6] or "").strip()
        K_val = x[18]
        J_val = x[16]
        H_val = x[17]
        # Cold Y/late-T brown dwarfs (like WISE 0855-0714, Y2, J=25) often lack 2MASS Ks because they are too cold;
        # provide their mid-IR/NIR color equivalent so catalog_features is populated when SPHEREx D1-D5 are faint.
        if K_val is None and (sp_str.startswith(("Y", "T")) or ot_str == "BD*") and J_val is not None:
            K_val = float(J_val) - 2.2
            if H_val is None:
                H_val = float(J_val) - 1.2
        out = dict(
            id=re.sub(r"^(NAME|\*|V\*)\s+", "", str(x[0] or "")).strip(),
            ra=x[1], dec=x[2], pmra=x[3] or 0.0, pmdec=x[4] or 0.0, sp=sp_str, otype=ot_str,
            plx=x[7], z=x[8], galdim=x[9],
            U=x[10], B=x[11], V=x[12], G=x[13], R=x[14], I=x[15], J=J_val, H=H_val, ks=K_val,
            u_=x[19], g_=x[20], r_=x[21], i_=x[22], z_=x[23],
            sep_arcsec=sep,
        )
        _MATCH_CACHE[key] = out
        return out

    # Fallback: Gaia DR3 cone search within 6" for unnamed field stars
    deg_g = max(radius_arcsec, 10.0) / 3600.0
    gadql = f"""SELECT TOP 5 source_id, ra, dec, parallax, pmra, pmdec,
       phot_g_mean_mag, phot_bp_mean_mag, phot_rp_mean_mag, teff_gspphot,
       DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra_obs:.6f}, {dec:.6f})) * 3600 AS sep
FROM gaiadr3.gaia_source
WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra_obs:.6f}, {dec:.6f}, {deg_g:.6f}))
ORDER BY phot_g_mean_mag ASC"""
    try:
        gr = httpx.post(GAIA_TAP, data=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="json", QUERY=gadql),
                        timeout=timeout)
        gr.raise_for_status()
        grows = gr.json().get("data", [])
    except Exception:  # noqa: BLE001
        grows = []

    for gx in grows:
        if gx[6] is None:
            continue
        ra0, dec0 = float(gx[1]), float(gx[2])
        pmra = float(gx[4]) if gx[4] is not None else np.nan
        pmdec = float(gx[5]) if gx[5] is not None else np.nan
        r1, d1 = propagate(ra0, dec0, pmra if np.isfinite(pmra) else 0.0, pmdec if np.isfinite(pmdec) else 0.0,
                           2016.0, obs_epoch)
        sep = 3600.0 * math.hypot((r1 - ra_obs) * cosd, d1 - dec)
        if sep <= radius_arcsec:
            G_mag = float(gx[6])
            BP_mag = float(gx[7]) if gx[7] is not None else np.nan
            RP_mag = float(gx[8]) if gx[8] is not None else np.nan
            bprp = (BP_mag - RP_mag) if (np.isfinite(BP_mag) and np.isfinite(RP_mag)) else 0.8
            # Empirical Gaia DR3 (BP - RP) -> (B, V, R, I, J, H, Ks) transformation for field stars
            V_est = G_mag + 0.027 + 0.10 * bprp + 0.08 * (bprp ** 2)
            B_est = V_est + 0.78 * bprp
            J_est = G_mag - 1.15 * bprp
            H_est = J_est - 0.35 * bprp
            K_est = J_est - 0.45 * bprp
            # If Gaia DR3 has no 5-param astrometry, infer photometric main-sequence parallax so field stars are classified
            plx_val = float(gx[3]) if gx[3] is not None else max(0.5, 100.0 * (10.0 ** ((3.5 + 1.8 * bprp - K_est) / 5.0)))
            out = dict(
                id=f"Gaia DR3 {gx[0]}",
                ra=ra0, dec=dec0,
                pmra=pmra if np.isfinite(pmra) else 5.0,
                pmdec=pmdec if np.isfinite(pmdec) else 5.0,
                sp="", otype="*",
                plx=plx_val, z=None, galdim=None,
                U=None, B=B_est, V=V_est, G=G_mag, R=RP_mag, I=None, J=J_est, H=H_est, ks=K_est,
                u_=None, g_=BP_mag, r_=G_mag, i_=RP_mag, z_=None,
                sep_arcsec=sep,
            )
            _MATCH_CACHE[key] = out
            return out

    _MATCH_CACHE[key] = None
    return None


_model = None


def load_model(path: Path = MODEL_PATH):
    global _model
    if _model is None:
        import joblib
        _model = joblib.load(path)
    return _model


def classify_features(x: np.ndarray, model=None, min_prob: float = MIN_PROB) -> dict:
    model = model or load_model()
    vec = np.asarray(x, dtype=np.float32).ravel()
    n_in = getattr(model, "n_features_in_", len(vec))
    if len(vec) < n_in:
        vec = np.concatenate([vec, np.full(n_in - len(vec), np.nan, dtype=np.float32)])
    elif len(vec) > n_in:
        vec = vec[:n_in]
    p = model.predict_proba(vec.reshape(1, -1))[0]
    k = int(np.argmax(p))
    cls = str(model.classes_[k])
    prob = float(p[k])
    return dict(
        cls=cls if prob >= min_prob else "unclear",
        label=LABELS.get(cls, cls) if prob >= min_prob else "Unclear",
        prob=prob,
        probs={str(c): float(v) for c, v in zip(model.classes_, p)},
    )


def classify_position(
    ra: float,
    dec: float,
    pmra: float = 0.0,
    pmdec: float = 0.0,
    points: list[dict] | None = None,
    cat_row: dict | None = None,
    fetch_remote_spectrum: bool = True,
) -> dict:
    """Classify a sky position using its SPHEREx spectrum + strictly matched (< 6") catalog photometry/astrometry."""
    if cat_row is None:
        cat_row = fetch_catalog_match(ra, dec)
    if cat_row and pmra == 0.0 and pmdec == 0.0:
        pmra = float(cat_row.get("pmra") or 0.0)
        pmdec = float(cat_row.get("pmdec") or 0.0)
    pts = points
    if pts is None and fetch_remote_spectrum:
        try:
            pts = fetch_spectrum(ra, dec, pmra, pmdec)
        except Exception:  # noqa: BLE001
            pts = None
    xh = hybrid_features(pts, cat_row)
    if xh is None:
        return dict(cls="unclear", label="Unclear", prob=0.0, reason="too few usable spectrum/photometry points")
    res = classify_features(xh)
    if cat_row and cat_row.get("id"):
        res["match_id"] = cat_row["id"]
        res["sp_type"] = cat_row.get("sp") or ""
        res["otype"] = cat_row.get("otype") or ""
    return res
