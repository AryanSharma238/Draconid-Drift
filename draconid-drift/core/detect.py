"""Moving / changing source detection on difference images, track linking, classification."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import ndimage

from .data import PIX_ARCSEC


def _sigma(img: np.ndarray) -> float:
    v = img[np.isfinite(img)]
    if v.size == 0:
        return np.inf
    return float(np.median(np.abs(v - np.median(v))) * 1.4826) or float(np.std(v)) or 1.0


def find_peaks(diff: np.ndarray, k: float = 5.0, edge: int = 3) -> pd.DataFrame:
    """Positive and negative >k-sigma local extrema of a difference image."""
    sm = ndimage.gaussian_filter(np.nan_to_num(diff), 0.8)
    s = _sigma(sm)
    rows = []
    for sign in (1, -1):
        a = sign * sm
        mx = ndimage.maximum_filter(a, size=5)
        pk = (a == mx) & (a > k * s)
        pk[:edge, :] = pk[-edge:, :] = pk[:, :edge] = pk[:, -edge:] = False
        for y, x in zip(*np.nonzero(pk)):
            rows.append({"x": float(x), "y": float(y), "snr": float(a[y, x] / s), "sign": sign})
    return pd.DataFrame(rows, columns=["x", "y", "snr", "sign"])


def detect_all(diff_cube: np.ndarray, mjds, w, k: float = 5.0,
               template: np.ndarray | None = None, static_ratio: float = 0.5) -> pd.DataFrame:
    """Peaks in every difference frame. If a template (median sky) is given, positive
    peaks sitting on a bright static source are rejected as subtraction residuals."""
    out = []
    tsig = _sigma(template) if template is not None else None
    for i, d in enumerate(diff_cube):
        p = find_peaks(d, k)
        if template is not None and len(p):
            tv = template[p["y"].astype(int), p["x"].astype(int)] / (tsig + 1e-9)
            dv = np.abs(d[p["y"].astype(int), p["x"].astype(int)]) / (_sigma(d) + 1e-9)
            # Residuals of bright static stars appear with BOTH signs; reject both.
            p = p[np.nan_to_num(tv) < static_ratio * dv]
        if len(p):
            sky = w.pixel_to_world(p["x"].values, p["y"].values)
            p["ra"], p["dec"] = sky.ra.deg, sky.dec.deg
            p["epoch"], p["mjd"] = i, mjds[i]
            out.append(p)
    cols = ["x", "y", "snr", "sign", "ra", "dec", "epoch", "mjd"]
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=cols)


def _linear(t: pd.DataFrame, tol_px: float = 1.2) -> bool:
    """Real movers travel in straight lines at constant speed over months."""
    if len(t) < 3:
        return True
    tt = t["mjd"].values - t["mjd"].values[0]
    for c in ("x", "y"):
        fit = np.polyval(np.polyfit(tt, t[c].values, 1), tt)
        if np.max(np.abs(fit - t[c].values)) > tol_px:
            return False
    return True


def link_tracks(det: pd.DataFrame, max_rate_px_per_day: float = 0.5,
                min_points: int = 2) -> pd.DataFrame:
    """Greedy nearest-neighbour linking of positive detections across epochs."""
    pos = det[det["sign"] > 0].sort_values("epoch")
    epochs = sorted(pos["epoch"].unique())
    tracks: list[list[int]] = []
    used: set[int] = set()
    for e0 in epochs:
        for i0, r0 in pos[pos["epoch"] == e0].iterrows():
            if i0 in used:
                continue
            chain, last = [i0], r0
            for e1 in [e for e in epochs if e > e0]:
                cand = pos[(pos["epoch"] == e1) & ~pos.index.isin(used | set(chain))]
                if cand.empty:
                    continue
                dt = max(cand["mjd"].iloc[0] - last["mjd"], 1e-3)
                dist = np.hypot(cand["x"] - last["x"], cand["y"] - last["y"])
                j = dist.idxmin()
                if 0.7 <= dist[j] <= max_rate_px_per_day * dt + 1.5:
                    chain.append(j)
                    last = cand.loc[j]
            need = max(min_points, min(3, len(epochs)))  # seen in every visit when ≤3 visits
            if len(chain) >= need and _linear(pos.loc[chain]):
                used.update(chain)
                tracks.append(chain)
    rows = []
    for tid, ch in enumerate(tracks, 1):
        t = pos.loc[ch]
        days = t["mjd"].iloc[-1] - t["mjd"].iloc[0]
        dpx = np.hypot(t["x"].iloc[-1] - t["x"].iloc[0], t["y"].iloc[-1] - t["y"].iloc[0])
        rate = dpx * PIX_ARCSEC / max(days, 1e-3)  # arcsec / day
        rows.append({"track": tid, "points": len(ch), "ra": t["ra"].iloc[0], "dec": t["dec"].iloc[0],
                     "rate_arcsec_per_day": rate, "rate_arcsec_per_yr": rate * 365.25,
                     "mean_snr": t["snr"].mean(), "xs": t["x"].tolist(), "ys": t["y"].tolist()})
    return pd.DataFrame(rows)


def classify(tracks: pd.DataFrame, gaia=None, asteroids=None, tol_arcsec: float = 12.0) -> pd.DataFrame:
    """Label tracks as known high-PM star, known asteroid, or new candidate."""
    if tracks.empty:
        return tracks
    from .catalogs import arcsec_sep

    labels = []
    for _, t in tracks.iterrows():
        label = "New candidate"
        if gaia is not None and len(gaia):
            hp = gaia[gaia["pm_total"] > 100]
            for _, g in hp.iterrows():
                if arcsec_sep(t["ra"], t["dec"], g.get("ra_t", g["ra"]), g.get("dec_t", g["dec"])) < tol_arcsec * 3:
                    label = f"Known high-PM star (Gaia {int(g['Source'])}, {g['pm_total']:.0f} mas/yr)"
                    break
        if label == "New candidate" and asteroids is not None and len(asteroids):
            for _, a in asteroids.iterrows():
                try:
                    if arcsec_sep(t["ra"], t["dec"], float(a["RA"]), float(a["DEC"])) < 60:
                        label = f"Known asteroid ({a.get('Name', 'SkyBoT match')})"
                        break
                except Exception:
                    continue
        labels.append(label)
    out = tracks.copy()
    out["classification"] = labels
    return out


def variables(cube: np.ndarray, w, k: float = 12.0) -> pd.DataFrame:
    """Fixed-position sources whose brightness changes across epochs."""
    if cube is None or len(cube) < 2:
        return pd.DataFrame()
    std = np.nanstd(cube, axis=0)
    mean = np.nanmean(cube, axis=0)
    score = std / (_sigma(cube[0]) + 1e-9)
    score[mean < 5] = 0  # only reasonably bright sources
    mx = ndimage.maximum_filter(np.nan_to_num(score), size=7)
    pk = (score == mx) & (score > k)
    ys, xs = np.nonzero(pk)
    if not len(xs):
        return pd.DataFrame()
    sky = w.pixel_to_world(xs, ys)
    return pd.DataFrame({"x": xs, "y": ys, "ra": sky.ra.deg, "dec": sky.dec.deg,
                         "variability_score": score[ys, xs]}).sort_values(
        "variability_score", ascending=False).head(10)
