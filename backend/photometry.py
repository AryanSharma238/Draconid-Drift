"""Forced aperture photometry (Task 06 F3 / Task 09 E4, E7). numpy only."""

import logging

import numpy as np

log = logging.getLogger(__name__)


def aperture_flux(
    data: np.ndarray,
    var: np.ndarray | None,
    x: float,
    y: float,
    r: float,
    r_in: float,
    r_out: float,
) -> tuple[float, float]:
    """Circular-aperture flux minus a median annulus background.

    (x, y) are 0-based pixel coordinates (column, row) of the aperture centre; pixels are included when
    their centres lie within the radius, so a moving aperture is just a different (x, y) per frame.
    flux = sum(data in r) - median(data in r_in..r_out) * n_pix.
    err  = sqrt(sum(var in r)); without `var`, the annulus scatter * sqrt(n_pix) is used.
    Returns (nan, nan) if the aperture holds any non-finite pixel or the annulus has no finite pixel
    (a partly masked or off-image aperture would silently bias the flux).
    """
    yy, xx = np.indices(data.shape)
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    ap = d2 <= r * r
    ann = (d2 >= r_in * r_in) & (d2 <= r_out * r_out)
    if not ap.any() or not np.isfinite(data[ap]).all():
        return float("nan"), float("nan")
    bkg_pix = data[ann & np.isfinite(data)]
    if bkg_pix.size == 0:
        return float("nan"), float("nan")
    n = int(ap.sum())
    flux = float(data[ap].sum() - np.median(bkg_pix) * n)
    if var is not None:
        err = float(np.sqrt(np.nansum(var[ap])))
    else:
        err = float(np.std(bkg_pix) * np.sqrt(n))
    return flux, err


def ring_flux(
    data: np.ndarray,
    var: np.ndarray | None,
    x: float,
    y: float,
    r_in: float,
    r_out: float,
    min_finite_frac: float = 0.9,
) -> tuple[float, float]:
    """Sum over the annulus r_in <= r <= r_out: photometry on the PSF wings of a star whose core is NaN.

    The image is already background-subtracted by process.py, so no local background is removed. Masked pixels
    are replaced by the mean of the finite ring pixels (so the sum always covers the whole ring); (nan, nan) when
    fewer than `min_finite_frac` of the ring is finite or the ring leaves the image (a clipped ring would change
    the flux scale between frames). err = sqrt(sum var), scaled the same way; without `var`, the ring scatter.
    This is a RELATIVE flux: the ring holds a wavelength-dependent fraction of the total flux.
    """
    h, w = data.shape
    if x - r_out < 0 or y - r_out < 0 or x + r_out > w - 1 or y + r_out > h - 1:
        return float("nan"), float("nan")
    yy, xx = np.indices(data.shape)
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    ring = (d2 >= r_in * r_in) & (d2 <= r_out * r_out)
    n = int(ring.sum())
    v = data[ring]
    fin = np.isfinite(v)
    if n == 0 or fin.mean() < min_finite_frac:
        return float("nan"), float("nan")
    flux = float(np.mean(v[fin]) * n)
    if var is not None:
        vv = var[ring]
        vf = np.isfinite(vv)
        if vf.any():
            return flux, float(np.sqrt(np.mean(vv[vf]) * n))
    return flux, float(np.std(v[fin]) * np.sqrt(n))
