"""Turn SPHEREx frames into a comparable Cube on a common north-up grid.

All real cutout fetching, flag masking, ZODI subtraction, and TAN WCS reprojection
are performed by `backend.fetch`, `backend.process`, and `backend.sequence`.
This module bridges `backend.models.Frame` and `Cube` for the verdict & flipbook UI.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS

from backend import config
from backend.models import Frame, Grid, ImageRef
from backend.models import make_grid as backend_make_grid
from backend.process import _reproject, bad_mask, group_by_pass

from . import SPHEREX_PIXEL_ARCSEC


@dataclass
class Cube:
    cube: np.ndarray      # (n, ny, nx) cleaned image, NaN = unusable
    var: np.ndarray       # (n, ny, nx) variance, NaN = unusable
    mjd: np.ndarray       # (n,) observation time
    wl: np.ndarray        # (n,) wavelength at field centre (micron)
    wcs: WCS              # common north-up TAN grid
    detector: str
    source: str           # "EC2-S3" | "S3-DIRECT" | "CACHE" | "SYNTHETIC"
    n_found: int = 0      # images the footprint index found

    @property
    def n(self):
        return self.cube.shape[0]

    def subset(self, idx):
        idx = np.asarray(idx)
        return Cube(self.cube[idx], self.var[idx], self.mjd[idx], self.wl[idx],
                    self.wcs, self.detector, self.source, self.n_found)


def make_grid(ra, dec, fov_arcsec, scale_arcsec=SPHEREX_PIXEL_ARCSEC):
    g = backend_make_grid(float(ra), float(dec), float(fov_arcsec) / 3600.0, float(scale_arcsec))
    return g.wcs(), (g.npix, g.npix)


def pixscale(cube) -> float:
    """Arcsec per pixel of a cube (6.15 at native resolution, coarser for wide fields)."""
    try:
        s = abs(float(cube.wcs.wcs.cdelt[1])) * 3600.0
        return s if s > 0 else SPHEREX_PIXEL_ARCSEC
    except Exception:
        return SPHEREX_PIXEL_ARCSEC


def frames_to_cube(frames: list[Frame], grid: Grid, detector: str, source: str = "S3-DIRECT", n_found: int = 0) -> Cube:
    """Convert processed `backend.models.Frame` objects into a `Cube`."""
    if not frames:
        raise ValueError("No usable frames after cleaning.")
    frames_sorted = sorted(frames, key=lambda f: f.ref.t_mjd)
    cube_arr = np.stack([np.asarray(f.data, dtype=float) for f in frames_sorted])
    var_arr = np.stack([
        np.asarray(f.var, dtype=float) if f.var is not None else np.ones_like(f.data, dtype=float)
        for f in frames_sorted
    ])
    bad = ~np.isfinite(cube_arr) | ~np.isfinite(var_arr) | (var_arr <= 0)
    cube_arr[bad] = np.nan
    var_arr[bad] = np.nan
    mjd_arr = np.array([float(f.ref.t_mjd) for f in frames_sorted], dtype=float)
    wl_arr = np.array([float(f.lambda_um) for f in frames_sorted], dtype=float)
    return Cube(cube_arr, var_arr, mjd_arr, wl_arr, grid.wcs(), detector, source, n_found or len(frames_sorted))


def cube_to_frames(cube: Cube) -> list[Frame]:
    """Wrap a `Cube` as `list[backend.models.Frame]` with pass_idx assigned via `backend.process.group_by_pass`."""
    det_num = int(cube.detector[-1]) if cube.detector and cube.detector[-1].isdigit() else 2
    bad_bit = 1 << config.FLAG_BITS_BAD[0]
    frames: list[Frame] = []
    for i in range(cube.n):
        ref = ImageRef(
            obs_id=f"frame_{i:03d}_D{det_num}",
            exposure_id=f"frame_{i:03d}",
            detector=det_num,
            t_mjd=float(cube.mjd[i]),
            release="DR1",
            version="l2b-v20",
            uri=f"frame_{i:03d}.fits",
        )
        data_i = np.asarray(cube.cube[i], dtype=np.float32)
        var_i = np.asarray(cube.var[i], dtype=np.float32)
        flags_i = np.where(np.isfinite(data_i), np.uint32(0), np.uint32(bad_bit))
        frames.append(
            Frame(
                ref=ref,
                data=data_i,
                var=var_i,
                flags=flags_i,
                lambda_um=float(cube.wl[i]) if np.isfinite(cube.wl[i]) else 1.5,
            )
        )
    return group_by_pass(frames)


def build_cube(raw, ra, dec, fov_arcsec, detector, source="SYNTHETIC", n_found=0,
               min_coverage=0.6):
    """Build a Cube from synthetic/demo cutout dicts using `backend.process` helpers."""
    det_num = int(detector[-1]) if detector and detector[-1].isdigit() else 2
    grid = backend_make_grid(float(ra), float(dec), float(fov_arcsec) / 3600.0, SPHEREX_PIXEL_ARCSEC)
    frames: list[Frame] = []
    for idx, c in enumerate(sorted(raw, key=lambda c: c["mjd"])):
        img = np.array(c["image"], dtype=np.float32)
        if c.get("zodi") is not None:
            img = img - np.asarray(c["zodi"], dtype=np.float32)
        var = np.array(c["variance"], dtype=np.float32)
        flags_in = np.asarray(c["flags"], dtype=np.uint32)
        bad = bad_mask(flags_in) | ~np.isfinite(img) | ~np.isfinite(var) | (var <= 0)
        if bad.all():
            continue
        _, med, _ = sigma_clipped_stats(img[~bad], sigma=3.0, maxiters=5)
        img = img - med
        img[bad] = np.nan
        var[bad] = np.nan
        r_img, fp = _reproject(img, c["wcs"], grid, "bilinear")
        r_var, _ = _reproject(var, c["wcs"], grid, "bilinear")
        if np.mean(fp > 0) < min_coverage:
            continue
        ok = np.isfinite(r_img) & np.isfinite(r_var) & (r_var > 0)
        r_img[~ok] = np.nan
        r_var[~ok] = np.nan
        ref = ImageRef(
            obs_id=f"syn_{idx:03d}_D{det_num}",
            exposure_id=f"syn_{idx:03d}",
            detector=det_num,
            t_mjd=float(c["mjd"]),
            release="DR1",
            version="l2b-v20",
            uri=f"syn_{idx:03d}.fits",
        )
        frames.append(
            Frame(
                ref=ref,
                data=r_img.astype(np.float32),
                var=r_var.astype(np.float32),
                flags=np.zeros_like(r_img, dtype=np.uint32),
                lambda_um=float(c.get("wavelength_um", np.nan)),
            )
        )
    return frames_to_cube(frames, grid, detector, source, n_found or len(raw))


def select_same_colour(cube: Cube, rel_width=0.04):
    """Keep only the frames inside the densest wavelength window (+/- rel_width/2)."""
    wl = cube.wl
    ok = np.isfinite(wl)
    if ok.sum() < 3:
        return cube
    best = None
    for w0 in wl[ok]:
        sel = ok & (np.abs(wl - w0) <= rel_width * w0 / 2)
        if best is None or sel.sum() > best.sum():
            best = sel
    return cube.subset(np.where(best)[0]) if best.sum() >= 3 else cube


def burst_groups(mjd, gap_days=config.PASS_GAP_DAYS):
    """Group frames into observing passes (gap > gap_days starts a new pass)."""
    order = np.argsort(mjd)
    groups, cur = [], [order[0]]
    for a, b in zip(order[:-1], order[1:]):
        if mjd[b] - mjd[a] > gap_days:
            groups.append(np.array(cur))
            cur = []
        cur.append(b)
    groups.append(np.array(cur))
    return groups


def flatten(cube, n_iter=4, clip=3.0):
    """Remove each frame's slowly varying background (a tilted plane fitted with sigma clipping),
    so the frames sit on the same flat level. Stars and movers are clipped out of the fit."""
    ny, nx = cube.cube.shape[1:]
    yy, xx = np.mgrid[0:ny, 0:nx]
    G = np.column_stack([np.ones(ny * nx), (xx.ravel() - nx / 2) / nx, (yy.ravel() - ny / 2) / ny])
    out = cube.cube.copy()
    for i in range(out.shape[0]):
        f = out[i].ravel()
        ok = np.isfinite(f)
        if ok.sum() < 50:
            continue
        for _ in range(n_iter):
            coef, *_ = np.linalg.lstsq(G[ok], f[ok], rcond=None)
            res = f - G @ coef
            sig = 1.4826 * np.nanmedian(np.abs(res[ok] - np.nanmedian(res[ok])))
            if not np.isfinite(sig) or sig <= 0:
                break
            ok = np.isfinite(f) & (np.abs(res) < clip * sig)
            if ok.sum() < 50:
                break
        out[i] = (f - G @ coef).reshape(ny, nx)
    return type(cube)(out, cube.var, cube.mjd, cube.wl, cube.wcs, cube.detector, cube.source, cube.n_found)
