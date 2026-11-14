import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


def make_fake_cutout(
    ra: float,
    dec: float,
    *,
    npix: int = 64,
    pixscale_arcsec: float = 6.2,
    rot_deg: float = 0.0,
    stars: list[tuple[float, float, float]] = (),
    noise: float = 1.0,
    background: float = 10.0,
    zodi: float = 5.0,
    seed: int = 0,
    psf_sigma_px: float = 1.0,
) -> fits.HDUList:
    """HDUList with PRIMARY, IMAGE, FLAGS, VARIANCE, ZODI. stars = [(ra, dec, flux)].
    IMAGE = background + zodi + stars (Gaussian PSF) + noise. Header WCS has rotation rot_deg."""
    rng = np.random.default_rng(seed)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(npix + 1) / 2, (npix + 1) / 2]
    s = pixscale_arcsec / 3600
    t = np.deg2rad(rot_deg)
    w.wcs.cd = np.array([[-s * np.cos(t), s * np.sin(t)], [s * np.sin(t), s * np.cos(t)]])
    yy, xx = np.mgrid[0:npix, 0:npix]
    img = np.full((npix, npix), background + zodi, dtype=np.float64)
    for sra, sdec, flux in stars:
        x, y = w.world_to_pixel_values(sra, sdec)
        img += (
            flux
            / (2 * np.pi * psf_sigma_px**2)
            * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * psf_sigma_px**2))
        )
    img += rng.normal(0, noise, img.shape)
    hdr = w.to_header()
    hdus = [
        fits.PrimaryHDU(),
        fits.ImageHDU(img.astype(np.float32), hdr, name="IMAGE"),
        fits.ImageHDU(np.zeros((npix, npix), np.int32), hdr, name="FLAGS"),
        fits.ImageHDU(np.full((npix, npix), noise**2, np.float32), hdr, name="VARIANCE"),
        fits.ImageHDU(np.full((npix, npix), zodi, np.float32), hdr, name="ZODI"),
    ]
    return fits.HDUList(hdus)
