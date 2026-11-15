"""Known targets: J2000 position + proper motion, and name aliases, so /api/resolve answers them locally.

Single source of truth (scripts/targets.py imports CATALOG from here). Positions/PM from SIMBAD via Sesame
(Gaia EDR3 where listed), queried 2026-11-14. A local hit skips the ~1.3 s Sesame round trip.
"""

import re

# name -> (ra_j2000_deg, dec_j2000_deg, pmra_masyr [= mu_alpha* cos dec], pmdec_masyr, display name)
CATALOG: dict[str, tuple[float, float, float, float, str]] = {
    "wise0855": (133.81890825, -7.247048, -8123.7, 673.2, "WISE 0855-0714"),
    "barnard": (269.4520769586187, 4.69336496657667, -801.551, 10362.394, "Barnard's Star"),
    "epsindb": (331.04414658852124, -56.78284419511611, 3981.977, -2466.832, "eps Ind B"),
    "rwaur": (76.95652603458, 30.40143817389, 7.23, -20.33, "RW Aur"),
    "rdra": (248.16761083295, 66.75497941722, -20.643, 6.149, "R Dra"),
    # SPRINT S3 extras: Sesame/SIMBAD J2000 position + PM, queried 2026-11-14 (backend.resolve.resolve_name_pm)
    "luhman16": (162.328814, -53.319466, -2767.502, 356.856, "Luhman 16"),
    "proxima": (217.42894222160578, -62.67949018907555, -3781.741, 769.465, "Proxima Cen"),
    "wolf359": (164.1205036259475, 7.01472313480167, -3866.338, -2699.215, "Wolf 359"),
    "teegarden": (43.25371580517042, 16.8812867194825, 3429.083, -3805.541, "Teegarden's Star"),
    "mira": (34.836633758, -2.977637673, 9.33, -237.36, "Mira (omi Cet)"),
    "betelgeuse": (88.792938991, 7.407063995, 27.54, 11.3, "Betelgeuse"),
    "m42": (83.8201, -5.3876, 1.67, -0.3, "Orion Nebula (M42)"),
    "pleiades": (56.65833, 24.17806, 19.997, -45.548, "Pleiades (M45)"),
    "m31": (10.6847083, 41.26875, 0.0, 0.0, "Andromeda (M31)"),
}

# Normalised alias -> CATALOG key. Normalise with `normalise()`: lower case, everything except a-z 0-9 + - . dropped.
_ALIASES: dict[str, tuple[str, ...]] = {
    "wise0855": (
        "wise 0855",
        "wise0855",
        "wise 0855-0714",
        "wise j085510.83-071442.5",
        "wisea j085510.74-071442.5",
        "wise j0855-0714",
        "w0855",
    ),
    "barnard": ("barnard's star", "barnards star", "barnard star", "gj 699", "hip 87937"),
    "epsindb": ("eps ind b", "epsilon indi b", "epsilon ind b", "eps indi b", "eps ind ba", "eps indi ba"),
    "rwaur": ("rw aur", "rw aurigae"),
    "rdra": ("r dra", "r draconis"),
    "luhman16": ("luhman 16", "luhman16", "wise 1049-5319"),
    "proxima": ("proxima cen", "proxima centauri", "proxima", "gj 551"),
    "wolf359": ("wolf 359", "wolf359", "gj 406"),
    "teegarden": ("teegarden's star", "teegardens star", "teegarden"),
    "mira": ("mira", "omicron ceti", "omi cet", "mira ceti"),
    "betelgeuse": ("betelgeuse", "alpha orionis", "alf ori"),
    "m42": ("m42", "messier 42", "orion nebula", "ngc 1976"),
    "pleiades": ("pleiades", "m45", "messier 45"),
    "m31": ("m31", "messier 31", "andromeda", "andromeda galaxy", "ngc 224"),
}


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9+\-.]", "", name.lower())


_BY_ALIAS: dict[str, str] = {normalise(a): k for k, aliases in _ALIASES.items() for a in aliases}


def lookup(name: str) -> dict | None:
    """{"name","ra","dec","epoch","pmra","pmdec"} (J2000, epoch 2000, mas/yr) for a known target, else None."""
    key = _BY_ALIAS.get(normalise(name))
    if key is None:
        return None
    ra, dec, pmra, pmdec, display = CATALOG[key]
    return {"name": display, "ra": ra, "dec": dec, "epoch": 2000.0, "pmra": pmra, "pmdec": pmdec}
