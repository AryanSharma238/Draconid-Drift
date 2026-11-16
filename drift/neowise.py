"""Brightness over time from NEOWISE (W1, 3.4 um), for stars too bright for SPHEREx to measure.

NEOWISE scanned the whole sky every six months from 2014 to 2024, and its profile-fit photometry stays usable
well past the point where SPHEREx saturates. We read its single-exposure table through IRSA's catalogue service,
group the exposures into visits (one visit = a few days every six months), and compare the visits.
"""
from __future__ import annotations

import io
import urllib.parse
import urllib.request

import numpy as np

GATOR = "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query"
EPOCH = 2019.0            # middle of the NEOWISE years: where to look for a fast-moving star


def _query(ra, dec, radius_arcsec, timeout):
    q = dict(catalog="neowiser_p1bs_psd", spatial="cone", radius=f"{radius_arcsec:.1f}", radunits="arcsec",
             objstr=f"{ra:.6f} {dec:+.6f}", outfmt="1", selcols="mjd,w1mpro,w1sigmpro,qual_frame,ra,dec")
    with urllib.request.urlopen(f"{GATOR}?{urllib.parse.urlencode(q)}", timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    rows = []
    for line in txt.splitlines():
        if not line.strip() or line[0] in "\\|":
            continue
        p = line.split()
        try:
            mjd, w1 = float(p[0]), float(p[1])
            e = float(p[2]) if p[2] != "null" else np.nan
            qf = float(p[3]) if p[3] != "null" else 0.0
        except (ValueError, IndexError):
            continue
        if qf > 0:
            rows.append((mjd, w1, e, float(p[4]), float(p[5])))
    return np.array(rows) if rows else np.zeros((0, 5))


def neowise_lc(ra, dec, pmra=0.0, pmdec=0.0, epoch=2026.0, timeout=40.0):
    """dict(pts=[[year, relative flux]], word, src) or None."""
    d = dec + pmdec * (EPOCH - epoch) / 3.6e6
    a = ra + pmra * (EPOCH - epoch) / 3.6e6 / max(np.cos(np.radians(d)), 1e-6)
    span = (abs(pmra) + abs(pmdec)) / 1000.0 * 6.0     # how far it moves in the six years either side of 2019
    try:
        x = _query(a, d, max(4.0, span + 4.0), timeout)
    except Exception:
        return None
    if len(x):                                       # keep only detections where the star actually is at that moment
        yr = 2000.0 + (x[:, 0] - 51544.5) / 365.25
        ed = dec + pmdec * (yr - epoch) / 3.6e6
        ea = ra + pmra * (yr - epoch) / 3.6e6 / np.maximum(np.cos(np.radians(ed)), 1e-6)
        sep = np.hypot((x[:, 3] - ea) * np.cos(np.radians(ed)), x[:, 4] - ed) * 3600.0
        x = x[sep < 2.5]
    if len(x) < 20:
        return None
    x = x[np.argsort(x[:, 0])]
    # visits: exposures within 5 days of each other
    cuts = np.where(np.diff(x[:, 0]) > 5)[0] + 1
    visits = [v for v in np.split(x, cuts) if len(v) >= 4]
    if len(visits) < 4:
        return None
    t = np.array([2000.0 + (np.median(v[:, 0]) - 51544.5) / 365.25 for v in visits])
    m = np.array([np.median(v[:, 1]) for v in visits])
    err = np.array([1.2533 * 1.4826 * np.median(np.abs(v[:, 1] - np.median(v[:, 1]))) / np.sqrt(len(v)) for v in visits])
    rel = 10 ** (-0.4 * (m - np.median(m)))
    spread = 1.4826 * np.median(np.abs(rel - 1))
    noise = float(np.median(err)) * 0.921 + 0.01                 # mag -> fraction, plus a 1% calibration floor
    if spread > max(3 * noise, 0.05):
        word = f"varies by about {100 * (np.percentile(rel, 90) - np.percentile(rel, 10)) / 2:.0f}%"
    else:
        word = f"no clear change (±{max(100 * spread, 1):.0f}%)"
    return dict(pts=[[round(float(a_), 3), round(float(b_), 3)] for a_, b_ in zip(t, rel)], word=word,
                src=f"NEOWISE 3.4 µm, {int(t.min())}–{int(t.max())}")
