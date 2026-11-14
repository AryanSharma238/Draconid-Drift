"""End-to-end: coordinates -> frames -> clean cube -> verdict -> player payload."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from . import irsa
from .clean import Cube, build_cube, flatten, select_same_colour
from .demo import MID_EPOCH, synthetic_raw
from .player_data import build_payload
from .targets import Target, mjd_to_year, propagate
from .verdict import assess

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache")


@dataclass
class Spec:
    name: str
    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float = 2016.0
    fov_arcsec: float = 240.0
    detector: str = "SPHEREx-D2"
    key: str = "custom"

    def as_target(self):
        return Target(self.key, self.name, self.ra, self.dec, self.pmra, self.pmdec,
                      self.epoch, self.fov_arcsec, self.detector, "")


def spec_from_target(t: Target):
    return Spec(t.name, t.ra, t.dec, t.pmra, t.pmdec, t.epoch, t.fov_arcsec, t.detector, t.key)


def field_center(spec: Spec):
    """Field centre = target position moved to the middle of the SPHEREx mission."""
    return propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)


# ----------------------------------------------------------------- cache ----
def cache_path(spec: Spec):
    ra, dec = field_center(spec)
    k = f"{spec.key}_{ra:.4f}_{dec:.4f}_{int(spec.fov_arcsec)}_{spec.detector}"
    return os.path.join(CACHE_DIR, hashlib.md5(k.encode()).hexdigest()[:12] + ".npz")


def save_cube(path, cube: Cube):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, cube=cube.cube.astype(np.float32), var=cube.var.astype(np.float32),
                        mjd=cube.mjd, wl=cube.wl, header=cube.wcs.to_header_string(),
                        detector=cube.detector, n_found=cube.n_found)


def load_cube(path):
    z = np.load(path, allow_pickle=False)
    wcs = WCS(fits.Header.fromstring(str(z["header"]), sep=""))
    return Cube(z["cube"].astype(float), z["var"].astype(float), z["mjd"], z["wl"], wcs,
                str(z["detector"]), "CACHE", int(z["n_found"]))


# ------------------------------------------------------------------ load ----
def load_frames(spec: Spec, demo=False, use_cache=True, progress=None, seed=0):
    """Returns (Cube, info dict). Order: demo -> cache -> IRSA."""
    ra, dec = field_center(spec)
    info = dict(center=(ra, dec), planes=None)
    if demo:
        raw, fra, fdec = synthetic_raw(spec.as_target(), spec.detector, fov_arcsec=spec.fov_arcsec,
                                       seed=seed)
        cube = build_cube(raw, fra, fdec, spec.fov_arcsec, spec.detector, "SYNTHETIC", len(raw))
        return cube, info
    path = cache_path(spec)
    if use_cache and os.path.exists(path):
        return load_cube(path), info
    planes = irsa.query_planes(ra, dec, detector=spec.detector)
    info["planes"] = irsa.count_by_detector(planes)
    size_deg = spec.fov_arcsec * 1.6 / 3600.0          # extra room for detector rotation
    raw, n_found = irsa.fetch_detector(planes, spec.detector, ra, dec, size_deg, progress=progress)
    cube = build_cube(raw, ra, dec, spec.fov_arcsec, spec.detector, "IRSA", n_found)
    try:
        save_cube(path, cube)
    except OSError:
        pass
    return cube, info


# ------------------------------------------------------------------- run ----
def run(spec: Spec, mode="target", same_colour=False, demo=False, n_null=300,
        progress=None, null_progress=None, seed=0):
    cube, info = load_frames(spec, demo=demo, progress=progress, seed=seed)
    cube = flatten(cube)
    if same_colour:
        cube = select_same_colour(cube)
    wcs = cube.wcs

    def pos(mjd):
        ra, dec = propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch,
                            mjd_to_year(mjd))
        x, y = wcs.world_to_pixel_values(ra, dec)
        return float(x), float(y)

    m = float(cube.mjd.mean())
    x0, y0 = pos(m)
    xa, ya = pos(m + 0.5 * 365.25); xb, yb = pos(m - 0.5 * 365.25)
    v_exp = (xa - xb, ya - yb)
    ny, nx = cube.cube.shape[1:]
    in_field = 0 <= x0 < nx and 0 <= y0 < ny

    verdict = assess(cube, mode=mode, v0=v_exp, center=(x0, y0), n_null=n_null,
                     seed=seed + 1, progress=null_progress)
    v_tr = (verdict["best"]["vx"], verdict["best"]["vy"]) if mode == "blind" else v_exp
    payload = build_payload(cube, pos if in_field else None, v_exp, v_tr)

    notes = []
    ra_c, dec_c = info["center"]
    if abs(spec.epoch - MID_EPOCH) > 1.0 and (spec.pmra or spec.pmdec):
        sh = 3600 * np.hypot((ra_c - spec.ra) * np.cos(np.radians(dec_c)), dec_c - spec.dec)
        notes.append(f"Coordinates are for epoch {spec.epoch:.1f}; moved {sh:.0f}\" "
                     f"({sh / 6.15:.0f} px) to the SPHEREx epoch before searching.")
    if cube.source != "SYNTHETIC":
        notes.append(f"{cube.n} of {cube.n_found or cube.n} {cube.detector} images used "
                     f"(wavelength {np.nanmin(cube.wl):.2f}-{np.nanmax(cube.wl):.2f} µm).")
    return dict(cube=cube, verdict=verdict, payload=payload, notes=notes, info=info,
                v_expected=v_exp, center=(x0, y0), in_field=in_field)
