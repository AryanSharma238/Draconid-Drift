# Recon notes (Task 01, sprint recon-lite)

## Status 2026-11-14
- R1 done: tutorial code copied into `scripts/spx_util.py` (TAP query, `wavelength_at` with `key="W"` + `sip=None`).
- R2 script written (`scripts/download_target.py`); NOT RUN: IRSA TAP + SIA down (see Outage log).
- R4 script written (`scripts/inspect_cutout.py`); needs a real cutout.
- R3, R6, R8, R10, R11: blocked on IRSA.

## Facts confirmed so far
- **HDU layout** (from the IRSA cutouts tutorial, to confirm on a real file in R4): PRIMARY, IMAGE, FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE. Primary header carries `VERSION` (processing version).
- **Wavelength**: `WCS(hdul["IMAGE"].header, fobj=hdul, key="W")`, then `.sip = None`, then `pixel_to_world(x, y)` → (wavelength, bandwidth). Track B's `backend/process.wavelength_at` must match this (check `sip=None`).
- SIA collections: `spherex_dr1`, `spherex_dr1_deep`. SIA lags ingestion ~1 day; TAP doesn't.

## Target positions (scripts/targets.py)
Source: SIMBAD via Sesame `-oxp` (J2000 + PM). Barnard's / eps Ind B / R Dra astrometry is Gaia EDR3.

| key | J2000 | pm (mas/yr) | centre @2025.9 | moved |
|---|---|---|---|---|
| wise0855 | 133.818908, −7.247048 | −8123.7, +673.2 | 133.759993, −7.242201 | 211″ |
| barnard | 269.452077, +4.693365 | −801.6, +10362.4 | 269.446290, +4.767917 | 269″ |
| epsindb | 331.044147, −56.782844 | +3982.0, −2466.8 | 331.096467, −56.800581 | 121″ |
| rwaur | 76.956526, +30.401438 | +7.2, −20.3 | 76.956586, +30.401292 | 0.6″ |
| rdra | 248.167611, +66.754979 | −20.6, +6.1 | 248.167235, +66.755024 | 0.6″ |

**Finding: the brief's WISE 0855 position (133.79513, −7.24515) is its WISE epoch ≈2010.45, not J2000.** At 2025.9 the object is ~126″ (~20 px) from the brief's position. A naive tool centred on the brief's coordinates puts the crosshair 20 px off. (Feeds the E1 "others centre on the wrong spot" demo.)

**Task 01 method problem:** "Gaia cone 60″ at the J2000 position" misses Barnard's and eps Ind B. They have moved > 60″ by Gaia's 2016.0 epoch (Barnard's ~166″). Cone at the PM-propagated 2016 position, or use the SIMBAD PM directly (done here).

## External service status (2026-11-14, ~14:00–15:00 local)
| service | status |
|---|---|
| IRSA TAP `/TAP/sync` | 502 Proxy Error / hangs > 60 s; `/TAP/availability` flaps 502↔200 |
| IRSA SIA `/SIA` | 502 Proxy Error |
| IRSA IBE `/ibe/` (file server) | 200 |
| CDS Sesame | works, 302 → mirror `vizier.idia.ac.za` (follow redirects) |
| SIMBAD TAP (astroquery.simbad) | broken: capabilities 404 |
| ESA Gaia archive | works |

## R4 / R6 results (from S3 mirror + Explanatory Supplement v2.1, 2026-11-14)
- S3 mirror `s3://nasa-irsa-spherex` (us-east-1, anonymous). Layout `{dr1|qr2|qr3}/level2/{week}_{block}/{version}/{det}/{file}`.
  dr1: 110 week dirs 2025W17_4B–2026W20_1A; qr2: 129 (2025W17–2026W29); qr3: 41 (2026W20–2026W40). QR2 and QR3 overlap.
- Regex (C2) passes 56/56 real names. Versions include `l2b_retry-v27-…`; retry dirs hold different exposures (0 overlap in 2025W17_4B D6: 1713 main vs 14 retry).
- Full DR1 D6 file (65 MB) HDUs: PRIMARY; IMAGE/FLAGS/VARIANCE/ZODI 2040² (each 26%); EPSF 441-row table of 33×33 float64 (3.9 MB, 6%); WCS-WAVE (5.8 KB).
- **Cutouts include the PSF/EPSF HDU unmodified** (data-products doc). At 0.1° the pixels are ~56 KB, so EPSF is ~98% of every cutout unless the service compresses it. Measure in R9.
- Sky WCS: RA---TAN-SIP, PC matrix (rotated), A_ORDER 3. Scale 6.157″/px (doc: 6.15″).
- Times: MJD-OBS/BEG/AVG/END present (MJD, e.g. 60789.75). Primary VERSION = pipeline software ('7.0.5'), not the l2b version.
- PSF: header `PSF_FWHM` (arcsec), 9.04″ in the sample, i.e. **σ ≈ 0.62 px (undersampled)**. Track D tests were built at σ = 1.0.
- Flags: Supplement Table 16 = MSKM cards in the header. Mask choice in config.py. SOURCE flags 47.6% of pixels in this frame.
- BUNIT MJy/sr. Wavelength at the D6 centre 4.695 µm via spx_util.wavelength_at.

## Motion: open issues for M5 (from Track D, at σ = 0.62 px)
- **PM formal errors are optimistic.** Undersampled PSF gives a pixel-phase centroid bias the error model ignores. Over 40 synthetic seeds: pmra pull rms 1.69 (7.5% beyond 3σ), pmdec pull rms 1.13. Decide in M5: inflate errors (~1.7× in RA) or add a phase-bias term. Do not publish raw formal errors.
- Ghost veto now only catches static-star chains with per-pass shifts above ~3.4 px (it was rejecting real movers at the true PSF).
- motion.py still has tunables as module constants (GHOST_TAIL_SIGMA, DEDUP_RADIUS_PX, REG_MAX_STARS, MASK_THRESHOLD, COVERAGE_FRAC, CENTROID_RECENTRE_ITERS). They violate the no-magic-numbers rule. Low priority; move them to config after the deadline-critical work.

## Deferred
- `SPHEREX_MID_EPOCH` (config): needs per-target frame times → TAP.
- `VALIDATION_DET` (config): needs R8 detection quality per detector → TAP.
- C2 `t_mjd` = `p.time_bounds_lower` units: FITS headers are MJD; the TAP column itself is unchecked → TAP.
- C10 WiseView names (R3a): cut to a 15-min look per SPRINT; not done.
- TAP `uri` format and release field: unknown until TAP returns (S3 path gives release as the first segment).

## TODO when IRSA returns
- `SELECT TOP 5 *` from spherex.plane / spherex.artifact → record all column names here.
- R2/R3 downloads; R4 on one D6 cutout; R6 regex on 20 URIs, MJD check, poly format, release dupes; R8 blink check.
