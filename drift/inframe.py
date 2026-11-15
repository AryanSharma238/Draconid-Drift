"""What is in the frame: named objects from SIMBAD, plus the brightest unnamed SPHEREx sources classified by drift/classify."""
import numpy as np
from scipy.ndimage import maximum_filter

from .classify import features, predict, _ap
from .movers import deep_stack
from .naming import _tap, _clean, _readable, _nice_names, EXTENDED

SPX_ARCSEC = 6.15
MATCH_ARCSEC = 14.0
BRIGHT_FRAC = 0.6          # unnamed sources at least this bright relative to the brightest are classified
MAX_ITEMS = 10

KIND = {  # SIMBAD object type -> plain words
    "*": "Star", "**": "Double star", "PM*": "Fast-moving star", "HPM": "Fast-moving star", "V*": "Variable star",
    "BD*": "Brown dwarf", "LM*": "Low-mass star", "RG*": "Red giant", "AB*": "Giant star", "s*r": "Supergiant",
    "C*": "Carbon star", "Mi*": "Pulsating giant", "LP*": "Pulsating giant", "YSO": "Young star", "TT*": "Young star",
    "Or*": "Young star", "Y*O": "Young star", "Em*": "Emission-line star", "Be*": "Be star", "WD*": "White dwarf",
    "EB*": "Eclipsing binary", "SB*": "Binary star", "PN": "Planetary nebula", "HII": "Star-forming nebula",
    "SNR": "Supernova remnant", "RNe": "Reflection nebula", "DNe": "Dark nebula", "GNe": "Nebula", "MoC": "Molecular cloud",
    "Cld": "Cloud", "SFR": "Star-forming region", "Cl*": "Star cluster", "OpC": "Open cluster", "GlC": "Globular cluster",
    "As*": "Stellar group", "G": "Galaxy", "GiG": "Galaxy", "GiP": "Galaxy", "GiC": "Galaxy in a cluster", "BiC": "Cluster's brightest galaxy",
    "SBG": "Starburst galaxy", "SyG": "Active galaxy", "Sy1": "Active galaxy", "Sy2": "Active galaxy", "AGN": "Active galaxy",
    "LIN": "Active galaxy", "EmG": "Emission-line galaxy", "IG": "Interacting galaxies", "PaG": "Galaxy pair", "GrG": "Galaxy group",
    "ClG": "Galaxy cluster", "QSO": "Quasar", "BLL": "Blazar", "rG": "Radio galaxy", "H2G": "Starburst galaxy",
    "LSB": "Faint galaxy", "bCG": "Compact galaxy", "IR": "Infrared source", "X": "X-ray source", "Rad": "Radio source",
    "Psr": "Pulsar", "XB*": "X-ray binary", "CV*": "Cataclysmic variable", "C?*": "Carbon star",
}
GUESS = {"star": "Star", "galaxy": "Galaxy", "other": "Nebula or cloud"}


def kind_of(otype):
    o = str(otype).strip('"').strip()
    if o in KIND:
        return KIND[o]
    if o.endswith("*") or o.endswith("?"):
        return "Star"
    return "Object"


SPEC = {"O": "hot blue", "B": "blue-white", "A": "white", "F": "yellow-white", "G": "Sun-like yellow", "K": "orange",
        "M": "red", "L": "L-type", "T": "T-type", "Y": "Y-type", "C": "carbon", "S": "S-type", "W": "Wolf-Rayet", "D": "white dwarf"}
LUM = [("Ia", "supergiant"), ("Ib", "supergiant"), ("III", "giant"), ("II", "bright giant"), ("IV", "subgiant"), ("V", "dwarf")]


def detail(kind, sp, morph):
    """Plain type plus what the database says about it: spectral class for stars, shape for galaxies."""
    if sp and kind not in ("Galaxy",) and sp[0] in SPEC:
        c = sp[0]
        if c in "LTY":
            return f"{kind}, {SPEC[c]} brown dwarf ({sp})" if "dwarf" not in kind.lower() else f"{kind}, {SPEC[c]} ({sp})"
        if c == "D":
            return f"White dwarf ({sp})"
        rest = sp[1:].lstrip("0123456789.+-/")
        lum = next((w for k, w in LUM if rest.startswith(k)), None)
        if lum is None and kind in ("Star", "Fast-moving star", "Variable star", "Double star", "Binary star", "Low-mass star"):
            return f"{kind}, {SPEC[c]} ({sp})"
        if lum == "dwarf" and c not in "KM":
            lum = "main-sequence"           # "dwarf" reads as white/brown dwarf for hotter stars
        what = f"{SPEC[c]} {lum}" if lum else SPEC[c]
        return f"{kind}, {what} ({sp})" if kind != "Star" else f"{what[0].upper() + what[1:]} star ({sp})"
    if morph and "alax" in kind:
        m = morph.lstrip("(").upper()
        shape = ("Elliptical" if m.startswith("E") else "Lenticular" if m.startswith("S0") or m.startswith("SB0") else
                 "Barred spiral" if m.startswith("SB") else "Spiral" if m.startswith("S") else "Irregular" if m.startswith("I") else None)
        if shape:
            return f"{shape} galaxy ({morph})" if kind == "Galaxy" else f"{kind}, {shape.lower()} ({morph})"
    return kind


def guess_detail(cls, fv):
    """Pipeline guess with what the SPHEREx spectrum and shape add (fv: concentration, size, ellipticity, slope, ...)."""
    if fv is None:
        return GUESS[cls]
    slope, size, ell = float(fv[3]), float(fv[1]), float(fv[2])
    if cls == "star":
        return "Star, red / cool" if slope < -1.6 else "Star, hot / blue" if slope > -0.7 else "Star, Sun-like colour"
    if cls == "galaxy":
        return ("Galaxy, elongated (disc seen edge-on)" if ell > 0.45 else "Galaxy, extended" if size > 0.7 else "Galaxy, compact")
    return "Nebula or dust cloud"


def _sep(a1, d1, a2, d2):
    return np.degrees(np.arccos(np.clip(np.sin(np.radians(d1)) * np.sin(np.radians(d2)) +
                                        np.cos(np.radians(d1)) * np.cos(np.radians(d2)) * np.cos(np.radians(a1 - a2)), -1, 1)))


def in_frame(cube, year=2026.0, stack=None):
    """List of dict(name, kind, ra, dec, size_arcsec, named, p). Always returns something when the frame has any source."""
    n, ny, nx = cube.cube.shape
    ra_c, dec_c = (float(v) for v in cube.wcs.wcs.crval)
    half = nx * SPX_ARCSEC / 3600 / 2
    from concurrent.futures import ThreadPoolExecutor
    cols = "oid, main_id, ra, dec, pmra, pmdec, otype, galdim_majaxis AS majax, nbref, sp_type, morph_type"
    circ = f"1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra_c:.6f},{dec_c:.6f},{half * 1.42:.6f}))"
    big = f"1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra_c:.6f},{dec_c:.6f},{half * 1.42 + 0.3:.6f}))"
    with ThreadPoolExecutor(3) as ex:      # well-studied objects, anything at all (for matching peaks), and big objects
        qs = [ex.submit(_tap, f"SELECT TOP 3000 {cols} FROM basic WHERE {circ} AND nbref >= 10", 12.0),
              ex.submit(_tap, f"SELECT TOP 3000 {cols} FROM basic WHERE {circ}", 12.0),
              ex.submit(_tap, f"SELECT TOP 100 {cols} FROM basic WHERE {big} AND galdim_majaxis > 0.5", 12.0)]
        rows, seen = [], set()
        for q in qs:
            for r in q.result() or []:
                if r.get("oid") not in seen:
                    seen.add(r.get("oid")); rows.append(r)
    sim = []
    for r in rows:
        try:
            a, d = float(r["ra"]), float(r["dec"])
        except (ValueError, TypeError, KeyError):
            continue
        pa = float(r["pmra"]) if (r.get("pmra") or "").strip() else 0.0
        pd = float(r["pmdec"]) if (r.get("pmdec") or "").strip() else 0.0
        d2 = d + pd * (year - 2000) / 3.6e6
        a2 = a + pa * (year - 2000) / 3.6e6 / max(np.cos(np.radians(d2)), 1e-6)
        x, y = cube.wcs.world_to_pixel_values(a2, d2)
        if not (0 <= x < nx and 0 <= y < ny):
            continue
        size = float(r["majax"]) * 60 if (r.get("majax") or "").strip() not in ("", "nan") else 0.0
        sim.append(dict(sp=(r.get("sp_type") or "").strip('"').strip(), morph=(r.get("morph_type") or "").strip('"').strip(), pmra=pa, pmdec=pd, oid=int(r["oid"]), main=r["main_id"], ot=str(r["otype"]).strip('"'), ra=a2, dec=d2, x=float(x), y=float(y),
                        size=size, nbref=int(r["nbref"] or 0)))
    if stack is None:
        stack, _ = deep_stack(cube)
    s = np.where(np.isfinite(stack), stack, np.nan)
    med = np.nanmedian(s)
    sig = 1.4826 * np.nanmedian(np.abs(s - med)) or 1e-6
    z = np.nan_to_num((s - med) / sig, nan=-99)
    ys, xs = np.nonzero((z == maximum_filter(z, size=5)) & (z > 6))
    img = np.nan_to_num(s - med)
    peaks = sorted(((_ap(img, x, y, 2.5)[0], int(x), int(y)) for x, y in zip(xs, ys)), reverse=True)
    out, used = [], set()
    # 1. named extended objects whose body covers a good part of the frame (a galaxy, a nebula): listed even without one peak
    for o in sorted(sim, key=lambda o: -o["nbref"]):
        if o["ot"] in EXTENDED and o["size"] >= 15 and o["nbref"] >= 10 and len(out) < 4:
            out.append(o); used.add(o["oid"])
    # 2. bright peaks: SIMBAD name when there is one within 6", otherwise the trained classifier
    if peaks:
        top = peaks[0][0]
        for f, x, y in peaks:
            if f < BRIGHT_FRAC * top or len(out) >= MAX_ITEMS:
                break
            near = [o for o in sim if np.hypot(o["x"] - x, o["y"] - y) * SPX_ARCSEC <= max(MATCH_ARCSEC, o["size"] / 2)]
            if near:
                o = max(near, key=lambda o: o["nbref"])          # the best-known name at this spot
                if o["oid"] not in used:
                    out.append(o); used.add(o["oid"])
                continue
            fv = features(stack, cube, float(x), float(y))
            cls, p = predict(fv) if fv is not None else ("star", 0.5)
            a, d = cube.wcs.pixel_to_world_values(x, y)
            out.append(dict(fv=fv, oid=None, main=None, ot=None, ra=float(a), dec=float(d), x=float(x), y=float(y), size=0.0, nbref=0,
                            guess=cls, p=round(float(p), 2)))
    # 3. room left: the best-studied named objects in the frame
    for o in sorted(sim, key=lambda o: -o["nbref"]):
        if len(out) >= MAX_ITEMS or o["nbref"] < 20:
            break
        inside_big = any(b["oid"] and b["size"] and np.hypot(b["x"] - o["x"], b["y"] - o["y"]) * SPX_ARCSEC < b["size"] / 2
                         and b["oid"] != o["oid"] for b in out)          # pieces of a galaxy already listed: skip
        if o["oid"] not in used and not inside_big and not o["main"].lstrip('"').startswith("[") \
                and kind_of(o["ot"]) != "Object":
            out.append(o); used.add(o["oid"])
    nice = _nice_names([o["oid"] for o in out if o["oid"]]) if any(o["oid"] for o in out) else {}
    res = []
    for o in out:
        if o["oid"]:
            name = nice.get(o["oid"]) or _readable(_clean(o["main"]))
            res.append(dict(name=name, kind=detail(kind_of(o["ot"]), o["sp"], o["morph"]), named=True, ra=o["ra"], dec=o["dec"], size=round(o["size"], 1),
                            pmra=round(o["pmra"], 1), pmdec=round(o["pmdec"], 1), epoch=year))
        else:
            res.append(dict(name=None, kind=guess_detail(o["guess"], o.get("fv")), named=False, ra=o["ra"], dec=o["dec"], size=0.0, p=o["p"]))
    return res
