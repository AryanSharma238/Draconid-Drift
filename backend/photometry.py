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
