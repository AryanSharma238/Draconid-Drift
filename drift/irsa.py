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
import time

import numpy as np
import requests
from astropy import units as u
from astropy.io import fits
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


def query_planes(ra, dec, timeout=90):
    """All overlapping SPHEREx spectral images at (ra, dec): list of dicts
    {uri, mjd, detector}. One TAP call covers all six detector arrays."""
    adql = f"""
    SELECT a.uri AS uri, p.time_bounds_lower AS mjd, p.energy_bandpassname AS det
    FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid
    WHERE 1 = CONTAINS(POINT('ICRS', {ra:.7f}, {dec:.7f}), p.poly)
    ORDER BY p.time_bounds_lower"""
    try:
        r = requests.post(TAP_URL, data=dict(REQUEST="doQuery", LANG="ADQL",
                                              FORMAT="csv", QUERY=adql), timeout=timeout)
        r.raise_for_status()
    except Exception as e:  # network, proxy, HTTP errors
        raise IrsaError(f"IRSA TAP query failed: {e}") from e
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if rows and "uri" not in rows[0]:
        raise IrsaError("IRSA TAP returned an unexpected table: " + r.text[:200])
    return [dict(uri=x["uri"], mjd=float(x["mjd"]), det=x["det"]) for x in rows]


def count_by_detector(planes):
    out = {d: 0 for d in DETECTORS}
    for p in planes:
        if p["det"] in out:
            out[p["det"]] += 1
    return out


def fetch_cutout(plane, ra, dec, size_deg, retries=4, timeout=120):
    """Download one cutout; dict of arrays + WCS, or None if empty / failed."""
    url = f"{BASE}{plane['uri']}?center={ra:.7f},{dec:.7f}d&size={size_deg:.5f}"
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
