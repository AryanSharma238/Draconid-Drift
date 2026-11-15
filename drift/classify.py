"""What kind of object is an unnamed SPHEREx source? A small model trained on SIMBAD-labelled sources.

Features (all measured on the SPHEREx frames themselves, so it works anywhere SPHEREx looked):
  concentration  flux inside 1.5 px / flux inside 4 px      (point sources are concentrated)
  size           second-moment radius minus the PSF's       (galaxies and nebulae are extended)
  ellipticity    from the second moments                     (galaxies are often elongated)
  slope          d log(flux) / d log(wavelength) across the frames' wavelengths (colour within the band)
  variability    scatter of the per-frame fluxes / their median, beyond the noise
  log_snr        how bright it is in the stack
Classes: star, galaxy, other (nebulae, clusters, clouds). Model: multinomial logistic regression,
weights in data/models/classifier.json, trained by scripts/train_classifier.py.
"""
from __future__ import annotations

import json
import os

import numpy as np

CLASSES = ("star", "galaxy", "other")
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models", "classifier.json")
FEATURES = ("concentration", "size", "ellipticity", "slope", "variability", "log_snr")

STAR_TYPES = {"*", "**", "PM*", "HPM", "V*", "BY*", "RS*", "Er*", "Fl*", "Ro*", "Pu*", "dS*", "RR*", "Ce*", "cC*", "Mi*", "LP*",
              "RG*", "AB*", "HB*", "WR*", "Be*", "s*r", "s*y", "s*b", "SB*", "EB*", "El*", "SN*", "WD*", "BD*", "LM*", "MS*",
              "Y*O", "TT*", "Or*", "Ae*", "C*", "S*", "OH*", "RB?", "HS*", "low*", "Em*", "V*?", "*i*", "Al*", "bL*", "WU*",
              "gD*", "Ir*", "Sy*", "PoC", "Y*?", "RG?", "AB?", "LP?", "C*?", "s?b", "s?r", "EB?", "Pu?", "BD?", "Psr", "XB*", "HXB",
              "LXB", "CV*", "No*", "BS*", "sg*", "Cl*?", "Y*?", "Ce?", "WD?", "PM?"}
GALAXY_TYPES = {"G", "GiC", "GiG", "GiP", "BiC", "IG", "PaG", "SBG", "SyG", "Sy1", "Sy2", "AGN", "LIN", "EmG", "H2G", "LSB", "bCG",
                "rG", "QSO", "BLL", "Bla", "G?", "AG?", "Q?", "GrG", "CGG", "ClG", "LeG", "LeQ", "Gr?", "C?G", "SC?", "BH"}
OTHER_TYPES = {"Cl*", "OpC", "GlC", "As*", "PN", "HII", "SNR", "RNe", "DNe", "EmO", "ISM", "MoC", "Cld", "GNe", "bub", "SFR",
               "cor", "glb", "IR", "smm", "mm", "cm", "Rad", "PN?", "HH", "out", "of?", "C?*", "OH?"}


def label_of(otype: str):
    o = str(otype).strip().strip('"')
    if o in GALAXY_TYPES:
        return "galaxy"
    if o in OTHER_TYPES:
        return "other"
    if o in STAR_TYPES or o.endswith("*"):
        return "star"
    return None


def _ap(img, x, y, r):
    ny, nx = img.shape
    x0, x1, y0, y1 = max(0, int(x - r - 1)), min(nx, int(x + r + 2)), max(0, int(y - r - 1)), min(ny, int(y + r + 2))
    yy, xx = np.mgrid[y0:y1, x0:x1]
    m = (np.hypot(xx - x, yy - y) <= r)
    sub = img[y0:y1, x0:x1]
    ok = m & np.isfinite(sub)
    return float(sub[ok].sum()), int(ok.sum()), int(m.sum())


def features(stack, cube, x, y, psf_sigma=0.62):
    """Feature vector for a source at array pixel (x, y). stack: noise-weighted mean image; cube: flattened Cube."""
    ny, nx = stack.shape
    if not (5 <= x < nx - 5 and 5 <= y < ny - 5):
        return None
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    ann = stack[(rr >= 6) & (rr <= 9) & np.isfinite(stack)]
    if ann.size < 10:
        return None
    bg = np.median(ann)
    sig = 1.4826 * np.median(np.abs(ann - bg)) or 1e-6
    img = stack - bg
    f1, n1, m1 = _ap(img, x, y, 1.5)
    f4, n4, m4 = _ap(img, x, y, 4.0)
    if f4 <= 0 or n4 < 0.6 * m4:
        return None
    conc = f1 / f4
    box = (rr <= 4) & np.isfinite(img)
    w = np.clip(img[box], 0, None)
    if w.sum() <= 0:
        return None
    dx, dy = xx[box] - x, yy[box] - y
    mxx, myy, mxy = (w * dx * dx).sum() / w.sum(), (w * dy * dy).sum() / w.sum(), (w * dx * dy).sum() / w.sum()
    size = np.sqrt(max(mxx + myy, 1e-6) / 2) - psf_sigma
    tr, det = mxx + myy, mxx * myy - mxy * mxy
    disc = np.sqrt(max(tr * tr / 4 - det, 0))
    l1, l2 = tr / 2 + disc, max(tr / 2 - disc, 1e-6)
    ell = 1 - np.sqrt(l2 / l1)
    snr = f4 / (sig * np.sqrt(m4))
    # per-frame fluxes: colour slope and variability
    fl, wl = [], []
    xi, yi = int(round(x)), int(round(y))
    for i in range(cube.n):
        fr = cube.cube[i]
        sub = fr[yi - 2:yi + 3, xi - 2:xi + 3]
        ring = fr[(rr >= 6) & (rr <= 9)]
        if not np.isfinite(sub).all() or not np.isfinite(cube.wl[i]):
            continue
        fl.append(float(sub.sum() - 25 * np.nanmedian(ring)))
        wl.append(float(cube.wl[i]))
    fl, wl = np.array(fl), np.array(wl)
    good = fl > 0
    if good.sum() >= 5 and np.ptp(np.log(wl[good])) > 0.05:
        slope = float(np.polyfit(np.log(wl[good]), np.log(fl[good]), 1)[0])
    else:
        slope = 0.0
    if fl.size >= 5 and np.median(fl) > 0:
        resid = fl - np.polyval(np.polyfit(np.log(wl), fl, 1), np.log(wl)) if np.ptp(wl) > 0 else fl - np.median(fl)
        var = float(1.4826 * np.median(np.abs(resid)) / np.median(fl))
    else:
        var = 0.0
    return np.array([conc, size, ell, np.clip(slope, -6, 6), np.clip(var, 0, 3), np.log10(max(snr, 1.0))])


_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is None and os.path.exists(MODEL_PATH):
        with open(MODEL_PATH) as f:
            m = json.load(f)
        _MODEL = dict(mu=np.array(m["mu"]), sd=np.array(m["sd"]), W=np.array(m["W"]), b=np.array(m["b"]), classes=m["classes"], quad=m.get("quad", False))
    return _MODEL


def predict(fv):
    """Returns (class, probability). Falls back to a simple shape rule if no model file is present."""
    m = load_model()
    if fv is None:
        return "unknown", 0.0
    if not np.isfinite(fv).all():
        fv = np.nan_to_num(fv)
    if fv[1] > 0.7 and fv[3] > 0.5:          # broad, flat-topped and diffuse: nebula / cloud, not a point or a galaxy
        return "other", 0.6
    if m is None:
        return ("star", 0.6) if fv[0] > 0.8 and fv[1] < 0.5 else ("galaxy", 0.5)
    x = quad(fv[None])[0] if m.get("quad") else fv
    z = ((x - m["mu"]) / m["sd"]) @ m["W"] + m["b"]
    p = np.exp(z - z.max())
    p /= p.sum()
    k = int(np.argmax(p))
    return m["classes"][k], float(p[k])


def quad(X):
    """Features plus all pairwise products, so the linear model can draw curved boundaries."""
    i, j = np.triu_indices(X.shape[1])
    return np.hstack([X, X[:, i] * X[:, j]])


def fit_softmax(X, y, n_class, l2=1e-2, iters=3000, lr=0.3, class_weight=True):
    """Plain multinomial logistic regression (numpy, full-batch gradient descent)."""
    n, d = X.shape
    W, b = np.zeros((d, n_class)), np.zeros(n_class)
    Y = np.eye(n_class)[y]
    cw = (n / (n_class * np.bincount(y, minlength=n_class).clip(1))) if class_weight else np.ones(n_class)
    sw = cw[y][:, None]
    for _ in range(iters):
        z = X @ W + b
        z -= z.max(1, keepdims=True)
        P = np.exp(z); P /= P.sum(1, keepdims=True)
        G = (P - Y) * sw / n
        W -= lr * (X.T @ G + l2 * W)
        b -= lr * G.sum(0)
    return W, b
