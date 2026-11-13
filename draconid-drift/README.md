# Draconid Drift — watching the sky, catching what drifts

**NASA Space Apps Challenge 2026 · "Planet X and SPHEREx"**

Draconid Drift is a public web tool for NASA's SPHEREx all-sky survey. Type in an object name or
coordinates and every SPHEREx visit to that patch of sky plays back as a flipbook. Then the tool
measures what moved, what changed and what the object is made of.

## What it does

| Tab | What you get |
|---|---|
| **Flipbook** | Every visit in true infrared colour (or single band, difference, raw), with smooth eased crossfades between visits. |
| **Difference** | Each visit minus all the others, computed band by band (Backyard Worlds method). |
| **Motion** | Sub-pixel astrometry: proper motion measured from SPHEREx alone, compared with the catalog value. |
| **Spectrum** | Motion-corrected forced photometry, giving a 0.75–5 µm spectrum with ice and molecule markers. |
| **Light curve** | Per-band normalised brightness over time, for variable stars. |
| **Discoveries** | Detection, track linking and classification against Gaia DR3 and (optionally) IMCCE SkyBoT asteroids. Exports CSV. |
| **Sky** | Aladin Lite context (WISE, 2MASS, DSS, Pan-STARRS) with Gaia and SIMBAD overlays. |
| **Data** | Per-frame metadata, contamination-flag fraction, CSV export and a shareable URL. |

Every view is linkable: `?target=WISE%200855&fov=8`.

## Validation on the official test targets (real SPHEREx data, Nov 2026)

| Target | Catalog proper motion | Measured by Draconid Drift | Agreement |
|---|---|---|---|
| Barnard's Star | 10.39″/yr | 10.15 ± 1.3″/yr | 98% |
| ε Indi B | 4.71″/yr | 4.59–4.98 ± 0.95″/yr | 94–97% |
| WISE 0855−0714 | 8.15″/yr | 9.49 ± 1.45″/yr | 84% (within 1σ) |
| RW Aurigae (variable) | ≈0 | 0.10″/yr | correctly stationary |

Results shift by a few percent between runs because frames are sampled per visit; raise "Frames per epoch & band" in Settings for tighter numbers.

ε Indi B's spectrum shows the textbook T-dwarf shape: methane absorption near 3.3 µm and a
4 µm peak.

## The insight that sets this tool apart

SPHEREx has no spectrograph. Its **linear variable filters** make the wavelength depend on where
a star lands on the detector, so each raw exposure of the same patch is a *different colour*. A
naive flipbook flickers, and stars look variable or seem to move when nothing happened.
Draconid Drift solves this:

1. **Wavelength-matched stacks.** Only same-band frames are compared, and the median within each
   visit removes cosmic rays.
2. **Band-matched differencing.** Each band is differenced against itself, then the per-band
   significance maps are combined.
3. **Static-source rejection and linear-motion tests.** These cut false movers. On WISE 0855 the
   false tracks dropped from 13 to 0.
4. **Motion-aware targeting.** The cutout centre and photometry aperture follow catalog proper
   motion to each exposure's date (WISE 0855 has moved about 2′ since its discovery position).

"Raw exposures" mode is kept on purpose, so judges and the public can see the problem.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
python -m pytest tests -q      # 5 offline tests (synthetic moving star)
```

## Deploy free on Streamlit Community Cloud

1. Push this folder to a public GitHub repo. `app.py` must be at the repo root.
2. Go to **share.streamlit.io**, choose **Create app**, then **Deploy a public app from GitHub**.
3. Pick the repo, branch `main`, main file `app.py`. Under **Advanced settings**, choose **Python 3.11 or 3.12**.
4. Click **Deploy**. The first build takes a few minutes (astropy and reproject are large).

## Architecture

```
app.py            Multipage UI (Home · Flipbook · Motion · Spectrum · Discoveries · Sky & Data), caching
core/data.py      Target resolution (built-in values → SIMBAD → Sesame), IRSA SIA queries,
                  server-side cutouts, per-pixel wavelength from the SPHEREx spectral WCS
core/imaging.py   Reprojection to a common north-up grid, stacking, differencing,
                  sub-pixel astrometry, photometry, asinh stretch, spline upsampling, GIF
core/detect.py    Peak finding, static-source rejection, track linking, classification
core/catalogs.py  Gaia DR3 (VizieR), IMCCE SkyBoT
core/style.py     Visual system: photo hero, floating pill nav, DM Sans, ink + amber
assets/hero.jpg   Hero image: SPHEREx colour composite of the Orion Nebula, rendered by this pipeline
```

- **Data:** collections `spherex_dr1` and `spherex_qr3` are merged and deduplicated, with
  `spherex_qr2` and `spherex_qr` kept as fallbacks. Every search hits IRSA live, so new weekly
  releases appear with no code changes.
- **Cutouts:** IRSA crops each image on the server (`?center=ra,dec&size=deg`), so only a few
  hundred KB per frame are transferred, not about 70 MB.

## Honest limitations

- Most of the sky has 2–4 visits so far (SPHEREx revisits about every 6 months). Motion precision
  therefore improves with each survey, and nearby stars' parallax wobble is not yet separated
  from proper motion.
- Even within one band, each visit samples a slightly different wavelength, so coloured stars
  leave faint difference residuals. Real movers are confirmed by the track and astrometry tests,
  not by the difference image alone.
- Discovery candidates are *candidates to inspect*, not confirmed discoveries.

## Credits

SPHEREx data: NASA/JPL-Caltech via the NASA/IPAC Infrared Science Archive. *"This publication
makes use of data products from the Spectro-Photometer for the History of the Universe, Epoch of
Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet Propulsion
Laboratory and the California Institute of Technology, and is funded by the National Aeronautics
and Space Administration."* Gaia DR3 (ESA), Aladin Lite and SIMBAD (CDS Strasbourg),
SkyBoT (IMCCE). Method inspired by Backyard Worlds: Planet 9 (Kuchner et al. 2017).
