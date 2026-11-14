"""Gaia DR3 queries (ESA archive via astroquery.gaia). Used by scripts/validate_gaia.py (Task 09 E5).

Both functions hit the network. Gaia DR3 positions are at ref_epoch 2016.0 (NOT J2000).
"""

import logging
import time

import requests
from astroquery.gaia import Gaia

from backend import config

log = logging.getLogger(__name__)

GAIA_MAX_ROWS = 5_000  # safety cap; the real count is ~143 (measured 2026-11-14). TOP 50000 gave HTTP 500


def _rows(table) -> list[dict]:
    """astropy Table -> list of plain dicts; masked values become NaN."""
    cols = table.colnames
    filled = {
        c: table[c].filled(float("nan"))
        if hasattr(table[c], "filled") and table[c].dtype.kind == "f"
        else table[c]
        for c in cols
    }
    return [{c: filled[c][i].item() for c in cols} for i in range(len(table))]


def _run(adql: str, asynchronous: bool) -> list[dict]:
    """Run an ADQL query, retrying config.FETCH_RETRIES times: the Gaia archive returns sporadic HTTP 500s
    and timeouts (seen 2026-11-14). Every failed attempt is logged; the last error is re-raised."""
    launch = Gaia.launch_job_async if asynchronous else Gaia.launch_job
    for attempt in range(1, config.FETCH_RETRIES + 1):
        try:
            return _rows(launch(adql).get_results())
        except (requests.exceptions.RequestException, OSError) as e:
            log.warning(
                "Gaia query attempt %d/%d failed: %s (%s)", attempt, config.FETCH_RETRIES, e, adql[:120]
            )
            if attempt == config.FETCH_RETRIES:
                raise
            time.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def query_truth(pm_min_masyr: float, g_min: float, g_max: float) -> list[dict]:
    """High-PM Gaia DR3 stars: total PM > pm_min_masyr, g_min < G < g_max.

    Columns: source_id, ra, dec, pmra, pmdec, phot_g_mean_mag, ref_epoch.
    """
    adql = (
        f"SELECT TOP {GAIA_MAX_ROWS} source_id, ra, dec, pmra, pmdec, phot_g_mean_mag, ref_epoch "
        "FROM gaiadr3.gaia_source "
        f"WHERE SQRT(POWER(pmra,2)+POWER(pmdec,2)) > {pm_min_masyr} "
        f"AND phot_g_mean_mag > {g_min} AND phot_g_mean_mag < {g_max}"
    )
    log.info("Gaia truth query: %s", adql)
    df = _run(adql, asynchronous=True)
    if len(df) >= GAIA_MAX_ROWS:
        log.warning("Gaia truth query hit the %d-row cap; the sample may be truncated", GAIA_MAX_ROWS)
    return df


def query_cone(ra: float, dec: float, radius_deg: float, g_max: float) -> list[dict]:
    """All Gaia DR3 sources within the cone with G < g_max. pmra/pmdec are NaN for 2-parameter sources.

    Columns: source_id, ra, dec, pmra, pmdec, phot_g_mean_mag, ref_epoch.
    """
    adql = (
        "SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag, ref_epoch FROM gaiadr3.gaia_source "
        f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{ra:.6f},{dec:.6f},{radius_deg:.6f})) "
        f"AND phot_g_mean_mag < {g_max}"
    )
    return _run(adql, asynchronous=False)
