"""Recon R4: python scripts/inspect_cutout.py <file.fits>"""

import fnmatch
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spx_util import wavelength_at

CARD_PATTERNS = (
    "DATE*",
    "MJD*",
    "EXPTIME",
    "BUNIT",
    "CD*",
    "CDELT*",
    "PC*",
    "CTYPE*",
    "A_ORDER",
    "*W",
    "VERSION",
)


def main(path: str) -> None:
    with fits.open(path) as h:
        total = sum(x.filebytes() for x in h)
        print(f"{'idx':>3} {'EXTNAME':<10} {'shape':<14} {'dtype':<8} {'bytes':>10}  %file")
        for i, x in enumerate(h):
            shape = None if x.data is None else x.data.shape
            dt = None if x.data is None else x.data.dtype
            print(
                f"{i:>3} {x.name:<10} {shape!s:<14} {dt!s:<8} {x.filebytes():>10}  {100 * x.filebytes() / total:.0f}%"
            )

        print("\n== PRIMARY cards")
        for k, v in h[0].header.items():
            if any(fnmatch.fnmatch(k, p) for p in CARD_PATTERNS):
                print(f"  {k} = {v!r}")
        hdr = h["IMAGE"].header
        print("\n== IMAGE cards")
        for k, v in hdr.items():
            if any(fnmatch.fnmatch(k, p) for p in CARD_PATTERNS):
                print(f"  {k} = {v!r}")

        w = WCS(hdr)
        cd = w.pixel_scale_matrix
        print(f"\npixel scale (CD/PC matrix): {np.sqrt(abs(np.linalg.det(cd))) * 3600:.4f} arcsec/px")
        print(f"has SIP: {w.sip is not None}")

        print("\n== FLAGS header (bit definitions)")
        for k, v in h["FLAGS"].header.items():
            c = h["FLAGS"].header.comments[k]
            if k.startswith(("MP_", "HIERARCH")) or "BIT" in k or "bit" in str(c).lower():
                print(f"  {k} = {v!r}  / {c}")
        flags = h["FLAGS"].data.astype(np.int64)
        set_bits = [b for b in range(32) if np.any(flags & (1 << b))]
        print(f"bits set anywhere in this cutout: {set_bits}")

        ny, nx = h["IMAGE"].data.shape
        ra, dec = w.pixel_to_world_values((nx - 1) / 2, (ny - 1) / 2)
        print(
            f"\ncentre ({ra:.6f}, {dec:.6f}); wavelength there: {wavelength_at(h, float(ra), float(dec)):.4f} µm"
        )

        img = h["IMAGE"].data
        zodi = h["ZODI"].data
        mi, mz = np.nanmedian(img), np.nanmedian(zodi)
        print(
            f"median(IMAGE)={mi:.4g}  median(ZODI)={mz:.4g}  median(IMAGE-ZODI)={np.nanmedian(img - zodi):.4g}"
        )
        print("  -> IMAGE ~ ZODI means zodi NOT subtracted (SUBTRACT_ZODI=True); IMAGE ~ 0 means subtracted")


if __name__ == "__main__":
    main(sys.argv[1])
