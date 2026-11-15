# Task 00 — Project setup

**Model:** any. **Time:** ~30 min. **Depends on:** nothing.

## Goal
A working repo skeleton: venv, pinned deps, config, models, timing helper, test fixtures, and passing empty tests.

## Steps

1. From `/Users/sasuke/Projects/nasa`, run `git init`.
2. Create `.gitignore`:
   ```
   .venv/
   __pycache__/
   *.pyc
   data/
   *.fits
   *.fits.gz
   .pytest_cache/
   .ruff_cache/
   notes/*.png
   .env
   ```
3. Create the directories: `backend/ frontend/ scripts/ tests/ tests/data/ notes/ data/index data/cache/raw data/cache/stacks`. Put an empty `backend/__init__.py` and `tests/__init__.py` in place.
4. Set up the venv and install. **Python 3.14** (decided 2026-11-14): 3.15 has no astropy wheel yet. Re-check 3.15 later with the dry-run below; switch only if every package installs from wheels.
   ```bash
   uv python install 3.14
   echo "3.14" > .python-version
   uv venv --python 3.14 .venv && source .venv/bin/activate
   # runtime deps — must ALL come from wheels (fail fast if not):
   uv pip install --only-binary :all: astropy astroquery pyvo reproject astropy-healpix numpy scipy \
       "httpx[http2]" fsspec s3fs fastapi "uvicorn[standard]" orjson pillow boto3
   uv pip freeze > requirements.txt
   # dev deps:
   uv pip install pytest pytest-asyncio ruff jupyter matplotlib
   uv pip freeze > requirements-dev.txt   # then delete the lines already in requirements.txt
   ```
   Also verify Linux ARM wheels for EC2 (t4g/c7g) before Task 07:
   ```bash
   uv pip install --dry-run --only-binary :all: --python-version 3.14 \
       --python-platform aarch64-unknown-linux-gnu -r requirements.txt --target /tmp/chk
   ```
   This must resolve with no "Wheels are required" errors. If it fails, use x86 instances (`t3.medium`/`c7i`) and record that in plan.md.
5. Create `backend/config.py` with the **exact content of contract C1**, plus the "Config constants to add" block from `tasks/08-motion.md` and the "Config additions" block from `tasks/09-edge.md`, plus `STACKS_BUCKET = ""` and `STACKS_PUBLIC_BASE = ""` (Task 04). Keep the values and `[VERIFY]` comments. After this commit, only Track A edits config (see SPRINT.md "Parallel execution").
6. Create `backend/models.py` with the dataclasses from contract C2. Implement `Grid.wcs()` and a constructor helper:
   ```python
   import math
   from astropy.wcs import WCS
   from backend import config

   def make_grid(ra: float, dec: float, size_deg: float,
                 pixscale_arcsec: float = config.PIXSCALE_ARCSEC) -> Grid:
       ra_s = (round(ra / config.COORD_SNAP_DEG) * config.COORD_SNAP_DEG) % 360.0
       dec_s = max(-90.0, min(90.0, round(dec / config.COORD_SNAP_DEG) * config.COORD_SNAP_DEG))
       npix = math.ceil(size_deg * 3600.0 / pixscale_arcsec)
       if npix % 2 == 0:
           npix += 1
       return Grid(ra=ra_s, dec=dec_s, size_deg=size_deg, npix=npix, pixscale_arcsec=pixscale_arcsec)

   # in Grid:
   def wcs(self) -> WCS:
       w = WCS(naxis=2)
       w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
       w.wcs.crval = [self.ra, self.dec]
       c = (self.npix + 1) / 2.0           # FITS 1-based centre pixel
       w.wcs.crpix = [c, c]
       s = self.pixscale_arcsec / 3600.0
       w.wcs.cdelt = [-s, s]
       return w
   ```
7. Create `backend/timing.py`:
   ```python
   import time, logging
   from contextlib import contextmanager
   log = logging.getLogger("timing")

   class Timer:
       """Collects stage durations. str(timer) -> Server-Timing header value."""
       def __init__(self) -> None:
           self.stages: dict[str, float] = {}
       @contextmanager
       def stage(self, name: str):
           t0 = time.perf_counter()
           try:
               yield
           finally:
               dt = (time.perf_counter() - t0) * 1000
               self.stages[name] = self.stages.get(name, 0.0) + dt
               log.info("stage=%s ms=%.1f", name, dt)
       def header(self) -> str:
           return ", ".join(f"{k};dur={v:.0f}" for k, v in self.stages.items())
   ```
8. Create `tests/fixtures.py`, a **synthetic SPHEREx-like cutout generator** used by all later tests (no network):
   ```python
   import numpy as np
   from astropy.io import fits
   from astropy.wcs import WCS

   def make_fake_cutout(ra: float, dec: float, *, npix: int = 64, pixscale_arcsec: float = 6.2,
                        rot_deg: float = 0.0, stars: list[tuple[float, float, float]] = (),
                        noise: float = 1.0, background: float = 10.0, zodi: float = 5.0,
                        seed: int = 0, psf_sigma_px: float = 1.0) -> fits.HDUList:
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
       for (sra, sdec, flux) in stars:
           x, y = w.world_to_pixel_values(sra, sdec)
           img += flux / (2 * np.pi * psf_sigma_px**2) * np.exp(-((xx - x)**2 + (yy - y)**2) / (2 * psf_sigma_px**2))
       img += rng.normal(0, noise, img.shape)
       hdr = w.to_header()
       hdus = [fits.PrimaryHDU(),
               fits.ImageHDU(img.astype(np.float32), hdr, name="IMAGE"),
               fits.ImageHDU(np.zeros((npix, npix), np.int32), hdr, name="FLAGS"),
               fits.ImageHDU(np.full((npix, npix), noise**2, np.float32), hdr, name="VARIANCE"),
               fits.ImageHDU(np.full((npix, npix), zodi, np.float32), hdr, name="ZODI")]
       return fits.HDUList(hdus)
   ```
   Real cutouts have more HDUs (PSF, WCS-WAVE) and a spectral WCS. Tests that need a wavelength monkeypatch `wavelength_at`.
9. Create `pyproject.toml` with:
   ```toml
   [tool.pytest.ini_options]
   markers = ["network: hits IRSA/Gaia; skipped by default"]
   addopts = "-m 'not network'"
   asyncio_mode = "auto"

   [tool.ruff]
   line-length = 110
   ```
10. Create `tests/test_setup.py`:
    - `make_grid(133.79513, -7.24515, 0.1)` gives npix odd; `ra == 133.795`; `dec == -7.245`.
    - `Grid.wcs().world_to_pixel_values(grid.ra, grid.dec)` == `((npix-1)/2, (npix-1)/2)` ± 1e-9 (0-based centre). **Why:** the crosshair contract C4 depends on this.
    - `make_fake_cutout` with one star: the brightest pixel sits within 1 px of the star's `world_to_pixel`.

11. Commit on `main`: `git add -A && git commit -m "Project skeleton"`. Parallel tracks branch from this commit.

## Done when
```bash
source .venv/bin/activate && python --version && pytest -q && ruff check backend tests
```
Prints `Python 3.14.x`, then 3 tests passed with 0 skipped, then ruff clean.

## Do NOT
- Add FastAPI app code (that's Task 04).
- Fill in [VERIFY] values (that's Task 01).
