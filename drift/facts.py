"""Plain facts about what the stage is pointed at: what it is, how far, how hot, how fast it moves, how old its light is.

Everything comes from catalogues (SIMBAD: type, spectral class, parallax, redshift, proper motion, size). Nothing is
guessed: a value the catalogue does not have is left out, and the page shows a dash.
"""
from __future__ import annotations

import math

LY_PER_PC = 3.26156

# rough surface temperature by spectral class and subclass (main sequence; giants are cooler at the same letter,
# so the page shows these with a "≈")
_T = {"O": (45000, 30000), "B": (30000, 10500), "A": (10000, 7500), "F": (7300, 6100), "G": (6000, 5300),
      "K": (5200, 3900), "M": (3850, 2300), "L": (2300, 1300), "T": (1300, 550), "Y": (500, 250)}


def temperature(sp: str):
    """≈ surface temperature in K from a spectral type like 'M4V', 'K1/5e', 'T6.5', or None."""
    if not sp:
        return None
    sp = sp.strip().lstrip("sd").lstrip("d")
    c = sp[:1].upper()
    if c not in _T:
        return None
    num = ""
    for ch in sp[1:]:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            break
    try:
        k = min(9.5, float(num)) / 10 if num else 0.5
    except ValueError:
        k = 0.5
    hot, cool = _T[c]
    return int(round((hot + (cool - hot) * k) / 50) * 50)


def light_years(plx_mas):
    if plx_mas is None or not (plx_mas > 0.05):
        return None
    return 1000.0 / plx_mas * LY_PER_PC


def lookback_years(z):
    """How long ago the light left, for a redshift (Planck 2018 cosmology)."""
    if z is None or not (z > 0.0005):
        return None
    try:
        from astropy.cosmology import Planck18
        return float(Planck18.lookback_time(z).to("yr").value)
    except Exception:
        return None


def constellation(ra, dec):
    try:
        from astropy.coordinates import SkyCoord, get_constellation
        c = str(get_constellation(SkyCoord(ra, dec, unit="deg")))
        return {"Ophiucus": "Ophiuchus"}.get(c, c)
    except Exception:
        return None


def _f(r, k):
    v = (r.get(k) or "").strip().strip('"')
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def simbad_row(ra, dec, radius_arcsec=15.0):
    """The best-known SIMBAD object within `radius_arcsec`, or None. (Two quick queries at once: sorting on the
    server is slow, so famous objects come from a well-cited-only query and anything else from a plain one.)"""
    from concurrent.futures import ThreadPoolExecutor
    from .naming import _tap
    cols = "oid, main_id, otype, sp_type, plx_value, rvz_redshift, pmra, pmdec, galdim_majaxis AS majax, nbref"
    cone = f"1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra:.6f},{dec:.6f},{radius_arcsec / 3600:.6f}))"
    with ThreadPoolExecutor(2) as ex:
        a = ex.submit(_tap, f"SELECT TOP 50 {cols} FROM basic WHERE {cone} AND nbref >= 50", 4.0)
        b = ex.submit(_tap, f"SELECT TOP 50 {cols} FROM basic WHERE {cone}", 4.0)
        rows = (a.result() or []) + (b.result() or [])
    if not rows:
        return None
    return max(rows, key=lambda r: int(r.get("nbref") or 0))


def catalogue_distance_ly(oid):
    """Median of SIMBAD's measured distances (Cepheids, tip of the red giant branch, ...) in light years, or None."""
    from .naming import _tap
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return None
    rows = _tap(f"SELECT dist, unit FROM mesDistance WHERE oidref = {oid}", 4.0) or []
    pc = []
    for r in rows:
        d, u = _f(r, "dist"), str(r.get("unit") or "").strip().strip('"').lower()
        if d and d > 0:
            pc.append(d * {"mpc": 1e6, "kpc": 1e3, "pc": 1.0}.get(u, float("nan")))
    pc = [p for p in pc if math.isfinite(p)]
    if not pc:
        return None
    pc.sort()
    return pc[len(pc) // 2] * LY_PER_PC


def describe(row):
    """Facts for one SIMBAD row (or None): kind, spectral class, distance, temperature, motion, redshift, size, light age."""
    if not row:
        return {}
    from .inframe import kind_of, detail
    ot = str(row.get("otype", "")).strip('"')
    sp = str(row.get("sp_type") or "").strip('"').strip()
    plx, z = _f(row, "plx_value"), _f(row, "rvz_redshift")
    pmra, pmdec, size = _f(row, "pmra"), _f(row, "pmdec"), _f(row, "majax")
    kind = kind_of(ot)
    ly = light_years(plx)
    galaxy = "alax" in kind or ot in ("QSO", "Bla", "BLL", "AGN", "Sy1", "Sy2", "SyG", "LIN", "rG", "ClG", "GrG")
    out = dict(kind=detail(kind, sp, ""), sp=sp or None, galaxy=bool(galaxy))
    if ly and not galaxy:
        out["distance_ly"] = ly
        out["light_years_ago"] = ly
    if not galaxy:
        t = temperature(sp)
        if t:
            out["temp_k"] = t
        if pmra is not None and pmdec is not None:
            out["motion_arcsec_yr"] = math.hypot(pmra, pmdec) / 1000.0
    else:
        if z is not None:
            out["redshift"] = z
        lb = lookback_years(z) if z is not None and z > 0.003 else None
        if lb is None:                         # nearby galaxies (or ones moving towards us, like Andromeda):
            ly = catalogue_distance_ly(row.get("oid"))   # redshift says little, use measured distances
            if ly:
                out["distance_ly"] = ly
                lb = ly
        if lb:
            out["light_years_ago"] = lb
        if size:
            out["size_arcmin"] = size
    return out


def target_facts(name, ra, dec, field_deg):
    """Facts for the stage's target. A patch of sky (no catalogue object at the centre) gets only its place."""
    row = simbad_row(ra, dec)
    f = describe(row)
    f["constellation"] = constellation(ra, dec)
    f["field_arcmin"] = field_deg * 60.0
    return f
