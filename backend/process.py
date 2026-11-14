"""Pure numpy/astropy processing: cutout bytes -> aligned Frame; pass grouping; co-adds."""

import dataclasses
import io
import logging

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.visualization import ZScaleInterval
from astropy.wcs import WCS
from reproject import reproject_interp

from backend import config
from backend.models import Frame, Grid, ImageRef

log = logging.getLogger("process")

LAMBDA_KEY = "LAMBDA_UM"  # set by fetch on cropped cutouts
SPECTRAL_WCS_KEY = "W"  # SPHEREx alternate WCS holding the per-pixel wavelength lookup


def wavelength_at(hdul: fits.HDUList, ra: float, dec: float) -> float:
    """µm at the pixel containing (ra, dec). Same code as scripts/spx_util.wavelength_at.

    The alternate "W" WCS is a lookup into WCS-WAVE indexed by FULL-frame pixels, so this
    must be called on an uncropped HDUList (fetch stores the result as LAMBDA_UM).
    """
    header = hdul["IMAGE"].header
    x, y = WCS(header).world_to_pixel(SkyCoord(ra, dec, unit="deg"))
    spectral_wcs = WCS(header, fobj=hdul, key=SPECTRAL_WCS_KEY)
    spectral_wcs.sip = None  # SIP applies to the sky WCS only
    wavelength, _bandwidth = spectral_wcs.pixel_to_world(x, y)
    return float(wavelength.to(u.micrometer).value)


def bad_mask(flags: np.ndarray, bits: tuple[int, ...] | None = None) -> np.ndarray:
    """True where any of `bits` is set (default config.FLAG_BITS_BAD)."""
    if bits is None:
        bits = config.FLAG_BITS_BAD
    m = np.zeros(flags.shape, bool)
    f = flags.astype(np.uint32)
    for b in bits:
        m |= ((f >> np.uint32(b)) & np.uint32(1)) == 1
    return m


def _reproject(arr: np.ndarray, in_wcs: WCS, grid: Grid, order: str) -> tuple[np.ndarray, np.ndarray]:
    return reproject_interp((arr, in_wcs), grid.wcs(), shape_out=(grid.npix, grid.npix), order=order)


def process_cutout(fits_bytes: bytes, ref: ImageRef, grid: Grid) -> Frame:
    """The ONLY entry point used by fetch/API. Runs in a worker process."""
    with fits.open(io.BytesIO(fits_bytes)) as hdul:
        hdr = hdul["IMAGE"].header
        img = np.array(hdul["IMAGE"].data, dtype=np.float32)
        flags_in = np.array(hdul["FLAGS"].data).astype(np.uint32)
        var_in = np.array(hdul["VARIANCE"].data, dtype=np.float32) if "VARIANCE" in hdul else None
        if config.SUBTRACT_ZODI and "ZODI" in hdul:
            img -= np.array(hdul["ZODI"].data, dtype=np.float32)
        img[bad_mask(flags_in)] = np.nan
        in_wcs = WCS(hdr)  # keeps SIP

        data, foot = _reproject(img, in_wcs, grid, "bilinear")
        data[foot == 0] = np.nan

        var = None
        if var_in is not None:
            var, vfoot = _reproject(var_in, in_wcs, grid, "bilinear")
            var[vfoot == 0] = np.nan
            var = var.astype(np.float32)

        fl, _ = _reproject(flags_in.astype(np.float64), in_wcs, grid, "nearest-neighbor")
        flags = np.nan_to_num(fl, nan=0.0).astype(np.uint32)

        _, med, _ = sigma_clipped_stats(data, sigma=3, maxiters=5)
        data = data - med

        # a cropped S3 cutout carries the wavelength computed from the full frame
        lam = float(hdr[LAMBDA_KEY]) if LAMBDA_KEY in hdr else wavelength_at(hdul, grid.ra, grid.dec)
    return Frame(ref, data.astype(np.float32), var, flags, lam)


def group_by_pass(frames: list[Frame], gap_days: float = config.PASS_GAP_DAYS) -> list[Frame]:
    """Sort by t_mjd; assign pass_idx in place (new pass when gap > gap_days). Returns sorted list."""
    out = sorted(frames, key=lambda f: f.ref.t_mjd)
    p = 0
    for i, f in enumerate(out):
        if i > 0 and f.ref.t_mjd - out[i - 1].ref.t_mjd > gap_days:
            p += 1
        f.pass_idx = p
    return out


def coadd(frames: list[Frame]) -> tuple[np.ndarray, np.ndarray | None]:
    """Inverse-variance weighted mean ignoring NaN. Returns (data, var_out = 1/den).

    Weights are 1/var where var is finite and > 0. If any frame lacks a variance
    map, all frames use equal weights (mixing weighted and unweighted is meaningless).
    var_out is None in that case.
    """
    if not frames:
        raise ValueError("coadd needs at least one frame")
    x = np.stack([f.data for f in frames]).astype(np.float64)
    use_var = all(f.var is not None for f in frames)
    if use_var:
        v = np.stack([f.var for f in frames]).astype(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            w = np.where(np.isfinite(v) & (v > 0), 1.0 / v, 0.0)
    else:
        w = np.ones_like(x)
    w = np.where(np.isfinite(x), w, 0.0)
    xz = np.where(np.isfinite(x), x, 0.0)
    num = (w * xz).sum(axis=0)
    den = w.sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den > 0, num / den, np.nan).astype(np.float32)
        var_out = np.where(den > 0, 1.0 / den, np.nan).astype(np.float32) if use_var else None
    return out, var_out


def coadd_by_pass(frames: list[Frame]) -> list[Frame]:
    """One Frame per pass: co-added data/var, OR'd flags, mean t_mjd and lambda."""
    ordered = group_by_pass(frames)
    out: list[Frame] = []
    for p in sorted({f.pass_idx for f in ordered}):
        grp = [f for f in ordered if f.pass_idx == p]
        det = grp[0].ref.detector
        data, var = coadd(grp)
        flags = np.zeros_like(grp[0].flags, dtype=np.uint32)
        for f in grp:
            flags |= f.flags.astype(np.uint32)
        ref = dataclasses.replace(
            grp[0].ref,
            obs_id=f"pass{p}_D{det}",
            exposure_id=f"pass{p}",
            t_mjd=float(np.mean([f.ref.t_mjd for f in grp])),
        )
        out.append(
            Frame(
                ref,
                data,
                var,
                flags,
                float(np.mean([f.lambda_um for f in grp])),
                pass_idx=p,
            )
        )
    return out


def zscale_limits(
    stack: list[np.ndarray], max_samples: int = config.ZSCALE_MAX_SAMPLES, seed: int = 0
) -> tuple[float, float]:
    """Stack-wide zscale (contract C9): finite pixels of all frames, subsampled with a fixed seed."""
    parts = [a[np.isfinite(a)].ravel() for a in stack]
    sample = np.concatenate(parts) if parts else np.array([])
    if sample.size == 0:
        raise ValueError("no finite pixels for zscale")
    if sample.size > max_samples:
        rng = np.random.default_rng(seed)
        sample = sample[rng.choice(sample.size, max_samples, replace=False)]
    lo, hi = ZScaleInterval().get_limits(sample)
    return float(lo), float(hi)
