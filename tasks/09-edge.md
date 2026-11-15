# Task 09 — Edge features: what other entries will miss

**Depends on:** Task 03 Part A, Task 08 M1–M4. Each item is independent, so do them in priority order. Each one is a separate lower-model session unless noted.
Tiers: **P1** = do today/tonight, cheap and high impact. **P2** = overnight batch. **P3/P4** = tomorrow, only if the MVP and Task 08 M6 are done.

---

## E1 (P1). Epoch-aware targets: propagate proper motion, moving crosshair
**What others miss:** catalogue coordinates are epoch J2000/2016. Barnard's Star has moved ~4.5′ since 2000, which puts it at the edge of a 0.1° cutout or outside it. WISE 0855 has moved ~1.5′.

Backend `backend/resolve.py`:
```python
def resolve_name_pm(name: str) -> dict | None:
    """astroquery.simbad.Simbad with add_votable_fields('pmra','pmdec')  [VERIFY column names in astroquery>=0.4.8:
    expected 'ra','dec' (deg, J2000 epoch), 'pmra','pmdec' (mas/yr)].
    Returns {"name", "ra", "dec", "epoch": 2000.0, "pmra": mas/yr or 0.0, "pmdec": mas/yr or 0.0}.
    Falls back to resolve_name() (Sesame, no PM) if Simbad fails."""

def propagate(ra, dec, pmra_masyr, pmdec_masyr, epoch_from, epoch_to) -> tuple[float, float]:
    """Linear: dec' = dec + pmdec*dt/3.6e6 ; ra' = ra + pmra*dt/3.6e6/cos(dec). dt in years.
    Fine for < 0.1° displacement; no parallax."""
```
- `/api/resolve` response (update contract C6): `{"name","ra","dec","epoch","pmra","pmdec","ra_now","dec_now","epoch_now"}`, where `epoch_now = config.SPHEREX_MID_EPOCH` (add it to config, `= 2025.9`; recon updates it to the actual mean epoch of the data).
- The frontend centres the request on `ra_now, dec_now` and puts `pmra, pmdec, epoch` (catalogue values) into the URL (contract C10: add `pmra`, `pmdec`, `epoch`).
- **Moving crosshair:** for each frame, compute the predicted position at the frame's `t_mjd` (`propagate` in JS: the same formula, plus the grid TAN projection from Task 06 F2's `worldToPixel`) and move the crosshair there. Draw the full predicted track as a faint dotted line.
- If the user enters raw RA/Dec (no PM), the crosshair is static (as before).

Tests:
- `propagate(Barnard J2000, pm, 2000, 2025.9)` moves ≈ 268″ north (10.36″/yr × 25.9 yr) ± 2″.
- A JS `propagate` vs Python `propagate` check on 3 cases (paste the Python outputs into test.html).
- Demo check: `?q=Barnard's Star` → the star sits under the crosshair in every pass.

## E2 (P1). Information-first stream order
**What others miss:** they stream 100 near-identical frames. The movement only shows across passes.

- `mode="all"` streams from L2 (precomputed): write the stored file so that the **pass co-adds come first**, as frames with `"kind":"pass"`, followed by the individual frames (`"kind":"single"`). Add the `kind` field to the contract C5 frame message, defaulting to "single".
- The frontend shows a blink of the pass co-adds as soon as they arrive (3–4 tiny frames → < 300 ms), with a toggle "Passes / All frames" that works the moment the singles are loaded.
- Live (non-precomputed) path: unchanged (singles stream as they arrive; pass co-adds are computed client-side via `meanFrames` once each pass is complete).

Test: in a stored stack, every `kind:"pass"` line precedes every `kind:"single"` line.

## E3 (P1). Robust co-adds + mesh background
**What others miss:** cosmic rays, ghosts and satellite glints in single frames become fake "movers" in co-adds. Zodiacal light gradients differ by season and leave large-scale residuals in differences.

In `backend/process.py`:
```python
def coadd(frames, clip_sigma: float = config.COADD_CLIP_SIGMA):   # REPLACES the plain mean from Task 03
    """Iterative (2 passes) sigma-clipped inverse-variance mean per pixel:
    1. m = weighted mean; r = (x - m)/sqrt(var) ; reject |r| > clip_sigma ; recompute.
    With < 3 inputs at a pixel: no clipping (plain weighted mean)."""

def mesh_background(img: np.ndarray, box: int = config.BKG_BOX_PX) -> np.ndarray:
    """Split into box×box tiles; per tile: sigma-clipped median of finite pixels (NaN if < 50% finite);
    fill NaN tiles from the nanmedian of their neighbours; upsample to full size with scipy.ndimage.zoom(order=1),
    cropped/padded to the image shape. Subtract this instead of the single scalar median in process_cutout step 8."""
```
Config: `COADD_CLIP_SIGMA = 4.0`, `BKG_BOX_PX = 16` (at 0.1° FOV that's ~4×4 tiles; for tiny FOVs where npix < 2·box, fall back to the scalar median).

Tests:
- A cosmic ray (+1000 in one frame of 10) → absent from the co-add (< 3σ).
- With all frames equal, the co-add equals the input (clipping is a no-op).
- A linear gradient background → `img - mesh_background(img)` has |residual| < 0.1·(gradient amplitude) away from the edges.
- Test 6 from Task 03 (co-add SNR) still passes.

## E4 (P1). λ-matched light curves (variability, done right)
**What others miss:** each frame is at a different λ, so raw photometry over time mixes the spectrum with variability. R Dra and RW Aur are test targets: blinking alone shows "something changed", while this quantifies it.

In `backend/motion.py` (reuse M1's matching):
```python
def lambda_matched_lightcurve(frames: list[Frame], x: float, y: float,
                              dlam_frac: float = config.LAMBDA_MATCH_FRAC) -> list[dict]:
    """For each frame i with λ-matches in OTHER passes:
         f_i, e_i = photometry.aperture_flux(frame_i.data, frame_i.var, x, y, r=2, r_in=4, r_out=6)
         f_t, e_t = same on the λ-matched template (coadd of matches)
         ratio = f_i / f_t ; err = ratio * sqrt((e_i/f_i)**2 + (e_t/f_t)**2)   (skip if f_t <= 3*e_t)
    Returns [{"t_yr","lambda_um","pass","ratio","err","dmag": -2.5*log10(ratio)}]."""
```
This needs `backend/photometry.py::aperture_flux` (spec in Task 06 F3; implement it here if F3 isn't done).
- Endpoint: `GET /api/lightcurve?ra&dec&size&det` → `{"points":[...], "summary":{"per_pass_dmag":[{pass, median_dmag, n}], "chi2_red_constant": float}}`. `chi2_red_constant` is the reduced χ² of the ratios against 1.0. A large value means variable.
- Precompute it for the targets (RW Aur, R Dra, and also the brown dwarfs, as a control).
- Frontend: a small SVG plot (time on x, Δmag on y, colour by λ) under the player when the target has a light curve.

Tests:
- A synthetic star with flux = g(λ) constant in time → per-pass median dmag ≈ 0 ± 0.02, and chi2_red ≈ 1. *Why: proves the spectral shape doesn't leak into the "variability".*
- The same star 2× brighter in pass 2 → pass-2 dmag ≈ −0.75 ± 0.05.

## E5 (P2, overnight). Validation at scale against Gaia (the "trust us" result)
**What others miss:** they show 1–3 hand-picked examples. We report recovery rates and PM accuracy over ~150 fields.

`scripts/validate_gaia.py`:
1. Truth set: Gaia DR3, `sqrt(pmra²+pmdec²) > 1500` mas/yr, `8 < phot_g_mean_mag < 17` [tune the bright limit after recon shows the saturation magnitude]. Sample 150 spread over the sky (e.g. at most 3 per HEALPix nside=4 cell). Save `data/validation/truth.csv`.
2. For each: centre = position propagated to `SPHEREX_MID_EPOCH`, size 0.1, det = `config.VALIDATION_DET` (default 3 [VERIFY: the detector where stars are best detected]).
   - Pipeline: find_images → get_frames → pass_stack → refine_registration → motion_search → measure_movers.
   - **Store the full stacks in L2 too** (they serve as precomputed fields for the demo).
3. Match: the mover nearest the truth's t0 position within 3 px → measured PM. Record `found`, `dpmra`, `dpmdec`, `sig` (the Δ normalised by the reported errors), `n_false` (other movers in the field without a Gaia counterpart: a Gaia cone search on the field with all sources, propagated, within 2 px).
4. Resumable (skip fields with a result row). Concurrency 4 fields; the global fetch semaphore protects IRSA.
5. Output:
   - `data/validation/results.csv`
   - `notes/validation_summary.md`: recovery fraction overall, and binned by G mag and by PM; median |Δpm|; fraction with |Δ|/σ < 3; false movers per field (median, 90th percentile).
   - Plots via matplotlib (scripts may use it): measured vs Gaia pmra and pmdec (1:1 line), and recovery vs G.
- **Start it by ~23:00 tonight**, on the laptop or EC2. Estimated: 150 fields × ~100 cutouts ≈ 15k cutouts ≈ 40–80 min at ~5 cutouts/s. Plus Gaia queries.
- **If recovery is poor:** don't hide it. Check registration offsets and signs first. Report what was found.

## E6 (P3). Candidate gallery (precomputed discovery feed)
**What others miss:** a tool that points users at what to look at, like Backyard Worlds, instead of making them guess coordinates.

1. `scripts/build_gallery.py`:
   - Inputs: the E5 fields, plus ~100 extra fields around UltracoolSheet T/Y dwarfs (det 6), plus ~100 random |b| > 30° fields (det 6). Same pipeline. Extra fields are optional if time is short.
   - Keep movers that **(a)** have no Gaia counterpart within 2 px of their propagated positions, **(b)** have per-pass detections ≥ 3σ in ≥ 2 passes individually, and **(c)** have motion_sig ≥ 4.
   - Score = motion_sig × min(snr_stack, 20) / 20.
   - Write `data/gallery.json`: `[{id, ra0, dec0, pm_total_arcsec_yr, pmra, pmdec, motion_sig, snr, field_url}]`, where `field_url` is the main-tool URL with params + `&hl=<mover index>`, sorted by score.
2. `frontend/gallery.html` + `gallery.js`: a card grid, 24 per page. Each card has a small canvas (draw pass co-adds from the L2 stack and blink them on hover), PM, score, and an "Inspect" link. Header text: "Motion candidates — unverified, for human inspection."
- Precomputed fields make every card instant.

## E7 (P4). Mover spectrum + methane index
**What others miss:** linking *motion* with *SPHEREx spectroscopy*. A moving source with a CH₄ dip at 3.3 µm is a T/Y dwarf candidate. That's a classification only SPHEREx can do.

- For each mover (Task 08 M4): forced photometry along its **fitted track** (aperture centred at x0 + v·(t − t0) per frame) in every frame of all 6 detectors → (λ, flux, err) points.
- `ch4_index = median flux(3.2–3.4 µm) / median flux(3.9–4.1 µm)`, with a bootstrap error. T/Y dwarfs should be ≪ 1; stars ≈ 1 [VERIFY on WISE 0855 vs field stars].
- Add `spectrum` and `ch4_index` to the C11 mover objects. The frontend's mover table gets a mini spectrum sparkline plus the index, and a badge "CH₄ absorption" if `ch4_index + 2σ < 0.7` [threshold tuned on WISE 0855 / known T dwarfs from E6].
- Requires all 6 detectors for the field (6× fetch). Only for precomputed fields.

Tests:
- A synthetic source with a 50% dip at 3.2–3.4 µm → ch4_index ≈ 0.5 ± 0.1.
- Moving aperture on a mover recovers ≥ 95% of the flux a fixed aperture gets on a static source of equal flux. *Why: a fixed aperture would lose flux on fast movers and bias the spectrum.*

---

## Config additions
```python
SPHEREX_MID_EPOCH = 2025.9    # [VERIFY: mean epoch of the data, from recon]
COADD_CLIP_SIGMA = 4.0
BKG_BOX_PX = 16
VALIDATION_DET = 3            # [VERIFY]
```

## Done when (per item)
- E1: `?q=Barnard's Star` shows the star under the moving crosshair in all passes (screenshot).
- E2: the stored-stack order test passes; the first pass blink is visible < 300 ms after load from local L2 (bench).
- E3: all its tests pass, and Task 03 + Task 08 tests still pass.
- E4: tests pass; `notes/` has R Dra and RW Aur light-curve screenshots plus the chi2 numbers.
- E5: `notes/validation_summary.md` + plots exist.
- E6: the gallery page lists ≥ 1 candidate, or states "0 candidates passed cuts" (also a valid result: report it).
- E7: WISE 0855 shows a CH₄ index lower than the field stars, or the honest negative result is recorded.
