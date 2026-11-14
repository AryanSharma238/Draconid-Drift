"""Preset targets, proper-motion propagation and time helpers.

All catalogue numbers come from the research notes (Gaia DR3 at epoch 2016.0 unless
stated). WISE 0855's listed position is a ~2010 position (epoch inferred from the
CatWISE offset), so it MUST be moved to the observation date before searching.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import SPHEREX_PIXEL_ARCSEC

MAS = 3.6e6  # mas per degree


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    ra: float          # deg, at `epoch`
    dec: float         # deg, at `epoch`
    pmra: float        # mas/yr, already multiplied by cos(dec) (Gaia convention)
    pmdec: float       # mas/yr
    epoch: float       # decimal year of (ra, dec)
    fov_arcsec: float
    detector: str      # suggested SPHEREx detector array
    story: str         # one-line "what you should see"
    epoch_note: str = ""


PRESETS = {
    "barnard": Target(
        "barnard", "Barnard's Star", 269.448503, 4.739420, -801.551, 10362.394, 2016.0,
        180, "SPHEREx-D2",
        "Fast mover (1.7 px/yr). Very bright: expect saturated core and flagged pixels."),
    "epsindi": Target(
        "epsindi", "Epsilon Indi B", 331.076453, -56.793812, 3987.41, -2505.35, 2016.0,
        240, "SPHEREx-D2",
        "Brown-dwarf pair (unresolved) moving with the very bright Eps Indi A ~65 px away."),
    "wise0855": Target(
        "wise0855", "WISE 0855-0714", 133.795129, -7.245147, -8118.9, 679.3, 2010.5,
        240, "SPHEREx-D6",
        "Faint, cold. Only seen beyond ~3.9 micron, so it needs stacking along its motion.",
        "Listed coordinates are a ~2010 position (inferred); the object is now ~133 arcsec west."),
    "rwaur": Target(
        "rwaur", "RW Aurigae", 76.956081, 30.401205, 2.64, -27.40, 2016.0,
        180, "SPHEREx-D2",
        "Does not move. Irregular dimming star; A/B pair 1.5 arcsec apart is unresolved."),
    "rdra": Target(
        "rdra", "R Draconis", 248.167378, 66.755007, -20.64, 6.15, 2016.0,
        180, "SPHEREx-D2",
        "Does not move. Mira variable (246 d period); expect saturation."),
}


def mjd_to_year(mjd):
    return 2000.0 + (np.asarray(mjd, float) - 51544.5) / 365.25


def year_to_mjd(year):
    return 51544.5 + (np.asarray(year, float) - 2000.0) * 365.25


def propagate(ra, dec, pmra, pmdec, epoch, to_epoch):
    """Linear proper-motion propagation (deg). pmra is mu_alpha* in mas/yr."""
    dt = np.asarray(to_epoch, float) - epoch
    dec1 = dec + pmdec * dt / MAS
    ra1 = ra + pmra * dt / MAS / np.cos(np.radians(0.5 * (dec + dec1)))
    return ra1, dec1


def target_position(t: Target, to_epoch: float):
    return propagate(t.ra, t.dec, t.pmra, t.pmdec, t.epoch, to_epoch)


def motion_px_per_year(pmra, pmdec, dec_deg, scale=SPHEREX_PIXEL_ARCSEC):
    """(dRA_arcsec/yr east-positive, dDec_arcsec/yr) -> sky motion in pixels/yr (mag)."""
    return float(np.hypot(pmra, pmdec) / 1000.0 / scale)
