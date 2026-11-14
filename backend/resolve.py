"""Name/coordinate resolution (contracts C3, C6 /api/resolve) and proper-motion propagation (Task 09 E1).

Name resolution uses CDS Sesame (-oxp XML), which returns SIMBAD proper motions. astroquery's Simbad
is NOT used (its TAP capabilities endpoint 404s as of 2026-11-14).
"""

import logging
import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from urllib.parse import quote

import astropy.units as u
import httpx
from astropy.coordinates import SkyCoord
from astropy.coordinates.name_resolve import NameResolveError

from backend import config

log = logging.getLogger(__name__)

SESAME_URL = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/S?"
SESAME_TIMEOUT_S = 15.0
CATALOG_EPOCH = 2000.0  # Sesame/SIMBAD J2000 position, epoch 2000
MAS_PER_DEG = 3.6e6

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_DECIMAL_PAIR = re.compile(rf"^\s*({_NUM})\s*[,\s]\s*({_NUM})\s*$")
_SEXA_HINT = re.compile(r"[hmsd:]", re.IGNORECASE)
_NUMERIC_TOKEN = re.compile(rf"^{_NUM}$")


def parse_coords(q: str) -> tuple[float, float] | None:
    """Contract C3. Decimal degrees pair, or sexagesimal (hourangle, deg). None means 'treat as a name'."""
    m = _DECIMAL_PAIR.match(q)
    if m:
        return float(m.group(1)), float(m.group(2))
    tokens = q.replace(",", " ").split()
    if _SEXA_HINT.search(q) or (len(tokens) == 6 and all(_NUMERIC_TOKEN.match(t) for t in tokens)):
        try:
            c = SkyCoord(q, unit=(u.hourangle, u.deg))
        except ValueError:
            return None  # e.g. "Barnard" contains 'd' but is a name
        return float(c.ra.deg), float(c.dec.deg)
    return None


@lru_cache(maxsize=4096)
def resolve_name(name: str) -> tuple[float, float] | None:
    """SkyCoord.from_name (Sesame), no proper motion. None if the name is unknown."""
    try:
        c = SkyCoord.from_name(name)
    except NameResolveError:
        return None
    return float(c.ra.deg), float(c.dec.deg)


def _tag_float(node: ET.Element, path: str) -> float | None:
    el = node.find(path)
    if el is None or el.text is None or not el.text.strip():
        return None
    return float(el.text)


def parse_sesame_xml(xml_text: str, query: str) -> dict | None:
    """Parse a Sesame -oxp document. Missing <pmRA>/<pmDE> give pm 0. None if nothing was resolved."""
    root = ET.fromstring(xml_text)
    for res in root.iter("Resolver"):
        ra, dec = _tag_float(res, "jradeg"), _tag_float(res, "jdedeg")
        if ra is None or dec is None:
            continue
        oname = (res.findtext("oname") or query).strip()
        oname = oname.removeprefix("NAME ")
        return {
            "name": oname,
            "ra": ra,
            "dec": dec,
            "epoch": CATALOG_EPOCH,
            "pmra": _tag_float(res, "pm/pmRA") or 0.0,
            "pmdec": _tag_float(res, "pm/pmDE") or 0.0,
        }
    return None


def resolve_name_pm(name: str) -> dict | None:
    """Sesame lookup returning {"name","ra","dec","epoch","pmra","pmdec"} (J2000/epoch 2000, mas/yr).

    If Sesame itself fails (network/HTTP/parse), log and fall back to SkyCoord.from_name with pm 0.
    None only when the name is genuinely unknown.
    """
    url = SESAME_URL + quote(name)
    try:
        r = httpx.get(url, follow_redirects=True, timeout=SESAME_TIMEOUT_S)
        r.raise_for_status()
        return parse_sesame_xml(r.text, name)
    except (httpx.HTTPError, ET.ParseError) as e:
        log.warning("Sesame failed for %r (%s): %s; falling back to SkyCoord.from_name", name, url, e)
    pos = resolve_name(name)
    if pos is None:
        return None
    return {"name": name, "ra": pos[0], "dec": pos[1], "epoch": CATALOG_EPOCH, "pmra": 0.0, "pmdec": 0.0}


def propagate(
    ra: float, dec: float, pmra_masyr: float, pmdec_masyr: float, epoch_from: float, epoch_to: float
) -> tuple[float, float]:
    """Linear PM propagation, no parallax. pmra is mu_alpha*cos(dec). Fine for < 0.1 deg displacement."""
    dt = epoch_to - epoch_from
    dec_new = dec + pmdec_masyr * dt / MAS_PER_DEG
    ra_new = ra + pmra_masyr * dt / MAS_PER_DEG / math.cos(math.radians(dec))
    return ra_new % 360.0, dec_new


def resolve(q: str) -> dict | None:
    """Full C6 /api/resolve payload for a name or coordinate string; None if not found."""
    coords = parse_coords(q)
    if coords is not None:
        info = {
            "name": q,
            "ra": coords[0],
            "dec": coords[1],
            "epoch": config.SPHEREX_MID_EPOCH,
            "pmra": 0.0,
            "pmdec": 0.0,
        }
    else:
        info = resolve_name_pm(q)
        if info is None:
            return None
    ra_now, dec_now = propagate(
        info["ra"], info["dec"], info["pmra"], info["pmdec"], info["epoch"], config.SPHEREX_MID_EPOCH
    )
    return {**info, "ra_now": ra_now, "dec_now": dec_now, "epoch_now": config.SPHEREX_MID_EPOCH}
