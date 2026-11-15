"""Shared recon helpers.

TAP query and wavelength-from-alternate-WCS code adapted from the IRSA tutorial
"Download a collection of SPHEREx Spectral Image cutouts" (Caltech-IPAC/irsa-tutorials,
tutorials/spherex/spherex_cutouts.md; authors V. Desai, J. Krick, A. Faisst, T. Raen, B. Sipőcz).
"""

import sys
from pathlib import Path

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config

RECON_DIR = config.DATA_DIR / "recon"


def adql_images(ra: float, dec: float, dets: tuple[int, ...] = config.DETECTORS) -> str:
    """Recon R2 query: every L2 image whose footprint contains (ra, dec), on the given detectors."""
    bands = ", ".join(f"'{config.BANDPASS_NAME.format(det=d)}'" for d in dets)
    return f"""
SELECT a.uri, p.planeid, p.time_bounds_lower, p.time_bounds_upper,
       p.energy_bandpassname, p.poly
FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid
WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
  AND p.energy_bandpassname IN ({bands})
ORDER BY p.time_bounds_lower
"""


def cutout_url(uri: str, ra: float, dec: float, size_deg: float) -> str:
    return f"{config.IRSA_BASE}{uri}?center={ra:.6f},{dec:.6f}d&size={size_deg}"


def wavelength_at(hdul: fits.HDUList, ra: float, dec: float) -> float:
    """Wavelength in µm at (ra, dec), from the IMAGE header's alternate 'W' WCS (tutorial code)."""
    header = hdul["IMAGE"].header
    x, y = WCS(header).world_to_pixel(SkyCoord(ra, dec, unit="deg"))
    spectral_wcs = WCS(header, fobj=hdul, key="W")
    spectral_wcs.sip = None  # the tutorial does this: SIP applies to the sky WCS only
    wavelength, _bandwidth = spectral_wcs.pixel_to_world(x, y)
    return float(wavelength.to(u.micrometer).value)
