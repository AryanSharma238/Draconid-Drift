"""IRSA access: TAP query for overlapping SPHEREx images + parallel cutout download.

Follows IRSA's "SPHEREx Spectral Image Cutouts" tutorial:
  * TAP join of spherex.plane and spherex.artifact (DR1 + QR3 are both in the tables)
  * cutout URL = <https://irsa.ipac.caltech.edu/ + artifact uri>?center=RA,DECd&size=DEG
  * cutout HDUs: IMAGE (MJy/sr, no zodi removed), FLAGS, VARIANCE, ZODI, WCS-WAVE
Cutouts are in native detector orientation; clean.py reprojects them north-up.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import io
import re
import time

import numpy as np
import requests
from astropy import units as u
from astropy.io import fits
from astropy.io.votable import parse_single_table
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord

TAP_URL = "https://irsa.ipac.caltech.edu/TAP/sync"
BASE = "https://irsa.ipac.caltech.edu/"
DETECTORS = [f"SPHEREx-D{i}" for i in range(1, 7)]
# approximate wavelength coverage of each detector array (micron), for labels only
DETECTOR_RANGE = {"SPHEREx-D1": "0.75-1.1", "SPHEREx-D2": "1.1-1.6", "SPHEREx-D3": "1.6-2.4",
                  "SPHEREx-D4": "2.4-3.8", "SPHEREx-D5": "3.8-4.4", "SPHEREx-D6": "4.4-5.0"}


class IrsaError(RuntimeError):
    pass


SIA_URL = "https://irsa.ipac.caltech.edu/SIA"
# IRSA's search (TAP) was returning 502 errors; SIA is a separate service that answers fast.
COLLECTIONS = ["spherex_dr1", "spherex_qr3"]     # first is required, the rest best-effort
_DET_IN_URL = re.compile(r"_\d?D(\d)_spx")


def _col(tab, name):
    """Column lookup that ignores letter case."""
    for c in tab.colnames:
        if c.lower() == name:
            return c
    raise KeyError(f"column {name!r} not in {list(tab.colnames)}")


def _sia_collection(ra, dec, collection, timeout, retries):
    """Same call IRSA's own SPHEREx tutorial uses (astroquery SIA)."""
    from astroquery.ipac.irsa import Irsa
    Irsa.TIMEOUT = timeout
    coord = SkyCoord(ra * u.deg, dec * u.deg)
    last = None
    for attempt in range(retries):
        try:
            tab = Irsa.query_sia(pos=(coord, 1 * u.arcsec), collection=collection)
            if len(tab) == 0:
                return []
            c_url, c_t = _col(tab, "access_url"), _col(tab, "t_min")
            c_det = next((c for c in tab.colnames if c.lower() == "energy_bandpassname"), None)
            rows = []
            for x in tab:
                url = str(x[c_url])
                det = str(x[c_det]) if c_det else ""
                if not det.startswith("SPHEREx"):
                    m = _DET_IN_URL.search(url)
                    det = f"SPHEREx-D{m.group(1)}" if m else ""
                rows.append(dict(uri=url, mjd=float(x[c_t]), det=det))
            return rows
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise IrsaError(f"IRSA SIA query for {collection} failed: {type(last).__name__}: {last}") from last


def query_planes(ra, dec, detector=None, timeout=120, retries=3):
    """Overlapping SPHEREx spectral images at (ra, dec): list of dicts {uri, mjd, det},
    sorted by time. `uri` is the full file URL."""
    rows = _sia_collection(ra, dec, COLLECTIONS[0], timeout, retries)
    for extra in COLLECTIONS[1:]:
        try:
            rows += _sia_collection(ra, dec, extra, 30, 1)
        except IrsaError:
            pass
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["mjd"]):
        if r["uri"] in seen or (detector and r["det"] != detector):
            continue
        seen.add(r["uri"])
        out.append(r)
    return out


def count_by_detector(planes):
    out = {d: 0 for d in DETECTORS}
    for p in planes:
        if p["det"] in out:
            out[p["det"]] += 1
    return out


def fetch_cutout(plane, ra, dec, size_deg, retries=4, timeout=120):
    """Download one cutout; dict of arrays + WCS, or None if empty / failed."""
    base = plane["uri"] if plane["uri"].startswith("http") else BASE + plane["uri"]
    url = f"{base}?center={ra:.7f},{dec:.7f}d&size={size_deg:.5f}"
    coord = SkyCoord(ra * u.deg, dec * u.deg)
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 500:        # IRSA: no overlap with this image
                return None
            r.raise_for_status()
            with fits.open(io.BytesIO(r.content)) as hd:
                img = hd["IMAGE"]
                if img.data is None or not np.isfinite(img.data).any():
                    return None
                wcs = WCS(img.header)
                x, y = wcs.world_to_pixel(coord)
                try:
                    swcs = WCS(img.header, fobj=hd, key="W")
                    swcs.sip = None
                    wave = float(swcs.pixel_to_world(x, y)[0].to(u.um).value)
                except Exception:
                    wave = float("nan")
                return dict(
                    mjd=plane["mjd"], det=plane["det"], wavelength_um=wave,
                    version=hd[0].header.get("VERSION", ""),
                    image=np.array(img.data, float),
                    flags=np.array(hd["FLAGS"].data),
                    variance=np.array(hd["VARIANCE"].data, float),
                    zodi=(np.array(hd["ZODI"].data, float) if "ZODI" in hd else None),
                    wcs=wcs)
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_detector(planes, detector, ra, dec, size_deg, workers=10, progress=None):
    """Parallel download of every cutout for one detector, with retries."""
    sel = [p for p in planes if p["det"] == detector]
    if not sel:
        raise IrsaError(f"No {detector} images overlap this position.")
    out, done = [], 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_cutout, p, ra, dec, size_deg) for p in sel]
        for f in cf.as_completed(futs):
            res = f.result()
            done += 1
            if res is not None:
                out.append(res)
            if progress:
                progress(done, len(sel))
    out.sort(key=lambda c: c["mjd"])
    if not out:
        raise IrsaError(f"All {len(sel)} {detector} cutouts failed or were empty.")
    return out, len(sel)
