import math
from dataclasses import dataclass

import numpy as np
from astropy.wcs import WCS

from backend import config


@dataclass(frozen=True)
class ImageRef:
    obs_id: str  # f"{exposure_id}_D{detector}", unique per exposure+detector
    exposure_id: str  # parsed from filename, e.g. "2025W19_2B_0346_1"  [VERIFY pattern]
    detector: int  # 1..6
    t_mjd: float  # p.time_bounds_lower, MJD  [VERIFY units]
    release: str  # "DR1" | "QR3" | ...
    version: str  # processing version parsed from filename, e.g. "l2b-v20-2025-247"
    uri: str  # path relative to IRSA_BASE, NO query string


@dataclass(frozen=True)
class Grid:
    ra: float  # snapped centre, deg
    dec: float  # snapped centre, deg
    size_deg: float
    npix: int  # = ceil(size_deg*3600 / PIXSCALE_ARCSEC), forced odd so centre is a pixel
    pixscale_arcsec: float

    def wcs(self) -> WCS:
        """TAN, CRVAL=(ra,dec), CRPIX=centre, north up, east left."""
        w = WCS(naxis=2)
        w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        w.wcs.crval = [self.ra, self.dec]
        c = (self.npix + 1) / 2.0  # FITS 1-based centre pixel
        w.wcs.crpix = [c, c]
        s = self.pixscale_arcsec / 3600.0
        w.wcs.cdelt = [-s, s]
        return w


@dataclass
class Frame:
    ref: ImageRef
    data: np.ndarray  # float32 (npix, npix), FITS orientation (row 0 = south), NaN = masked/no data
    var: np.ndarray | None  # float32 same shape, or None
    flags: np.ndarray  # uint32 same shape (nearest-neighbour reprojected)
    lambda_um: float  # wavelength at the grid centre
    pass_idx: int = -1  # assigned by group_by_pass, 0-based


def make_grid(
    ra: float, dec: float, size_deg: float, pixscale_arcsec: float = config.PIXSCALE_ARCSEC
) -> Grid:
    ra_s = (round(ra / config.COORD_SNAP_DEG) * config.COORD_SNAP_DEG) % 360.0
    dec_s = max(-90.0, min(90.0, round(dec / config.COORD_SNAP_DEG) * config.COORD_SNAP_DEG))
    npix = math.ceil(size_deg * 3600.0 / pixscale_arcsec)
    if npix % 2 == 0:
        npix += 1
    return Grid(ra=ra_s, dec=dec_s, size_deg=size_deg, npix=npix, pixscale_arcsec=pixscale_arcsec)
