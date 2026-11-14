# Task 06 — Features (in value order; each is its own session)

**Model:** lower model OK, one feature per session. **Depends on:** Task 05.
Do the features **in order**. Each is self-contained. The user decides where to stop. After each one, run `pytest -q` and the frontend `test.html`.

---

## F1. Custom A − B time arithmetic
**Why:** subtracting epochs cancels static sources, so movers appear as +/− pairs. This is the core discovery aid in the brief.

Frontend only (`frontend/js/arith.js` (pure) + UI in app.js):
```js
export function sumFrames(frames, idxs)  // per-pixel sum over finite values; NaN if none finite
export function meanFrames(frames, idxs) // per-pixel mean over finite values
export function subtract(a, b)           // a - b, NaN-propagating
```
UI: a filmstrip of thumbnails (8 px per source pixel max, drawn with the current stretch). Click a thumbnail to cycle it through none → A → B. Buttons: "Show A − B" (mean − mean), "Show sum(A)", "Clear". The result is drawn with `drawDiff` and robust σ limits (contract C9). URL keys: `a=0,1,2&b=7,8` (frame indices).
Tests (test.html):
- A static star in all frames → mean(A) − mean(B) is within 3σ of noise at the star's position.
- A star at p1 in A and p2 in B → positive peak at p1, negative at p2.
- `subtract` with NaN → NaN.

## F2. Gaia DR3 + CatWISE2020 overlay, propagated per frame epoch
**Why:** it lets users tell "known moving star" from "uncatalogued candidate".

Backend `backend/catalogs.py` + route `GET /api/catalog?ra&dec&size`:
1. Gaia (astroquery `Gaia.launch_job_async`), within the cone radius `size*0.75` deg:
   ```sql
   SELECT source_id, ra, dec, pmra, pmdec, parallax, phot_g_mean_mag, ref_epoch
   FROM gaiadr3.gaia_source
   WHERE 1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{ra},{dec},{r}))
   ```
2. CatWISE2020 via IRSA TAP (table `catwise_2020`; [VERIFY column names in an initial `SELECT TOP 1 *`]): `source_name, ra, dec, pmra, pmdec, w1mpro, w2mpro`. Note: CatWISE PM is in arcsec/yr, and the reference epoch is MJD 57170 [VERIFY].
3. Response: `{"gaia":[{id,ra,dec,pmra_masyr,pmdec_masyr,epoch_jyear,g}], "catwise":[{...,epoch_jyear,w1,w2}]}`. Cache in memory (LRU 2048) **and** on disk keyed by snapped (ra,dec,size). `Cache-Control: public, max-age=604800`.
4. Precompute in batch: `preprocess_catalog.py` also writes `catalog/{key}.json` to S3 and L2.

Frontend `frontend/js/overlay.js`:
- `positionAt(src, jyear)`: `dra = pmra_masyr/3.6e6 * (jyear - epoch) / cos(dec)`, `ddec = pmdec_masyr/3.6e6 * (jyear - epoch)`. That's linear PM; parallax is ignored (< 1″, sub-pixel).
- Convert to grid pixels with the TAN projection (same math as `Grid.wcs`, contract C4 orientation). Write `worldToPixel(ra, dec, header)` in JS and test it against Python output for 3 points.
- Draw an SVG circle (radius 2 px × zoom) per source **for the current frame's epoch**, so markers move with the frame. Gaia = cyan circles, CatWISE = magenta squares. Hover → tooltip with id, mag and PM.
- URL keys: `gaia=1`, `catwise=1`.
Tests:
- Python: `/api/catalog` for Barnard's Star returns a source with PM > 10000 mas/yr (network).
- JS: `positionAt` for a 10″/yr eastward PM after 1 yr moves ~1.6 px left at 6.2″/px.
- Visual: in pass mode, the Barnard's Star marker stays on the star across passes. Screenshot it into `notes/`.

## F3. Spectrum on the fly
**Why:** "extra kudos" in the brief. SPHEREx's unique value is 102 bands.

Backend `GET /api/spectrum?ra&dec&size` (the clicked position becomes the cutout centre):
1. For det in 1..6: refs = find_images; get frames (reuses the L1 cache).
2. Forced photometry per frame at the grid centre: circular aperture radius 2 px, sum minus background annulus 4–6 px median × area. σ from VARIANCE (sum of var in aperture).
   - Implement this in `backend/photometry.py` with numpy only: `aperture_flux(data, var, x, y, r, r_in, r_out) -> (flux, err)`.
3. Response: `{"points":[{"lambda_um","flux","err","t_mjd","det","pass"}], "unit":"<BUNIT>"}`, sorted by λ.
4. Stream it as NDJSON too (one point per line), since it can take ~6× a single-detector load.

Frontend `frontend/js/spectrum.js`:
- Click on the canvas → compute the clicked (ra, dec) via the inverse TAN → call the API → draw an SVG scatter plot (λ on x, flux on y, error bars, colour by pass).
- No chart library: hand-written SVG with axes, ticks and labels.
- "Download CSV" button (build the CSV in JS and show it in a `<textarea>` to copy if blob downloads are blocked).
Tests:
- `aperture_flux` on a synthetic Gaussian star (flux 1000, σ 1 px, r = 3) recovers ≥ 98% of the flux.
- Pure background gives ≈ 0 ± 3σ.
- A synthetic flat-spectrum source over fake frames at different λ gives a flat spectrum within errors. *Why: proves we don't inject wavelength-dependent bias.*

## F4. Wavelength filter
**Why:** use all 102 bands rather than just 6 detectors.

Frontend only: a dual-handle range slider `lam_min..lam_max` (0.75–5.0 µm). Frames whose `lambda_um` falls outside the range are hidden from playback and excluded from medians, sums and pass co-adds.
- Client-side pass co-add within the range: group the visible frames by `pass` and use `meanFrames`. A server-side co-add would ignore the filter.
- Show "n frames in range".
- URL keys: `lam_min`, `lam_max`.
Test: frames with λ = [1, 2, 3] and range 1.5–2.5 → only index 1 visible.
**Note:** this only makes sense in `mode=all`, since pass co-adds already mix λ. With a filter set, the UI forces `mode=all` and shows why.

## F5. Molecule-band presets + ratio images
**Why:** a creative use of 102 bands, grounded in the physics (brief: "Think creatively…").
**Gate:** recon R5 must show ≥ 2 frames per pass in each window at the test targets. Drop any preset that fails, and record that in notes.

Presets (put them in `frontend/js/presets.js` as data, not code):
```js
export const PRESETS = {
  ch4: { label: "Methane (brown dwarfs)", band: [3.2, 3.4], cont: [3.9, 4.1], dets: [5] },   // [VERIFY dets from recon]
  h2o: { label: "Water ice",               band: [2.9, 3.1], cont: [2.6, 2.8], dets: [4] },
  co2: { label: "CO₂ (comets, ices)",      band: [4.2, 4.35], cont: [4.0, 4.15], dets: [6] },
  co:  { label: "CO",                      band: [4.6, 4.75], cont: [4.45, 4.6], dets: [6] },
};
```
- Selecting a preset loads the needed detectors (`mode=all`), makes a band co-add and a continuum co-add **per pass**, and shows `band / continuum` (ratio; set NaN where continuum < 3σ).
- Colormap: diverging around 1.0.
- Display modes: band | continuum | ratio.
- URL key: `preset=ch4`.
Test: synthetic frames where the source is 0.3× in band and 1× in continuum → ratio ≈ 0.3 at the source and ≈ 1 for a flat source.
Visual check: WISE 0855 is darker in the CH₄ ratio than field stars (`notes/` screenshot). If it isn't, report that honestly. Don't tune until it "looks right".

## F6. "Cold object" colour view
Frontend: an RGB composite from three per-pass co-adds: D2 → blue, D4 → green, D6 → red, each with its own zscale stretch. Toggle "Colour". Loading the extra detectors happens lazily.
Test: a pixel bright only in D6 renders red-dominant.

## F7. Flags overlay + metadata panel
- A per-bit checkbox list from `FLAG_BIT_NAMES` (served by `/api/health` as `flag_bits`). Pixels with any checked bit get a semi-transparent colour overlay (mode=all only; flags are absent in pass mode).
- A metadata panel for the current frame: obs_id, UTC date, MJD, detector, λ, release, pass, n_coadd.
Test: a flags array with bit 3 set at pixel 5, and bit 3 checked → overlay alpha > 0 at pixel 5 only.

## F8. Moving-source hint (not ML)
`frontend/js/hint.js`:
1. For each pass co-add, find peaks: pixels > 5σ (robust) that are local maxima in 3×3. Centroid each over 3×3.
2. Match peaks between the first and last pass within 4 px.
3. A pair is a **candidate** if the shift is ≥ 1 px, *and* no Gaia/CatWISE source (propagated, F2) lies within 2 px of either position.
4. Draw a dashed yellow arrow from old → new. The label says "candidate – inspect", **never "discovery"**.
Tests:
- A synthetic static field plus one mover → exactly one candidate.
- The same mover with a catalogued counterpart → zero candidates.

## F9. Aladin Lite context panel
- Lazy-load Aladin Lite v3 from a pinned CDN URL only when the user clicks "Sky context". It must not load on page start (page weight budget).
- Centre on the target, FOV = 4 × size, survey `P/2MASS/color`. Draw the cutout footprint as a box.
- Optional: a "Blink with WISE" layer (HiPS `P/unWISE/color-W2-W1W2-W1`) for a longer baseline.

## F10. GIF export
- Use `gif.js` (pinned CDN, lazy-loaded) or a minimal hand-written GIF encoder.
- Render each frame at the current zoom/stretch with a timestamp label.
- Show the result as an `<img>`; the user saves it with right-click (programmatic downloads may be blocked in embeds).

---

## WiseView parity check (after the last feature you do)
Go through `notes/wiseview_parity.md`. For each item: done (where), or dropped (why). Commit the updated file.
