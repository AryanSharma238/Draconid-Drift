# Task 08 — Motion algorithms (the differentiator)

**Model:** lower model OK for M1–M4; they're pure numpy and tested on synthetic data. **Strong model** should review M3/M4 sign conventions and run M5 (real-data validation).
**Depends on:** Task 00 and Task 03 Part A (`Frame`, `coadd`, `group_by_pass`). It needs **no network and no API**, so it can run in parallel with Tasks 03C/04/05.
**File:** `backend/motion.py`. **Tests:** `tests/test_motion.py`. Add new constants to `backend/config.py` (listed at the end).

## Why this matters
Generic tools blink cutouts. SPHEREx frames are narrow-band, and each pixel has its own wavelength, so naive differences are dominated by colour, not motion. These algorithms:
1. subtract **wavelength-matched** templates;
2. **search** for movers by shift-and-stack across survey passes;
3. **measure** proper motion with errors, validated against Gaia.

## Conventions (read carefully; most bugs will be here)
- All arrays are in **FITS orientation** (row 0 = south), on the common `Grid`, before the wire flip of contract C4.
- Pixel coordinates are `(x, y)` = (column, row), 0-based. Grid CDELT1 < 0, so **+x = west (RA decreasing)** and **+y = north**.
- Velocities are `(vx, vy)` in **px/yr** on the grid. Convert to sky:
  - `pmra_cosdec_masyr = -vx * pixscale_arcsec * 1000`
  - `pmdec_masyr = +vy * pixscale_arcsec * 1000`
- Time: `t_yr = 2000.0 + (t_mjd - 51544.5) / 365.25` (Julian years). The reference epoch `t0` = weighted mean of the pass times.
- NaN = no data. Every routine must ignore NaN, never propagate it into a whole stack, and treat a pixel with no data in any input as NaN in the output.

---

## M1. Wavelength-matched residuals (`lambda_matched_residuals`)
```python
def lambda_matched_residuals(frames: list[Frame],
                             dlam_frac: float = config.LAMBDA_MATCH_FRAC) -> list[Frame]:
    """For each frame i (frames already have pass_idx set):
         matches = [j for j in frames if j.pass_idx != i.pass_idx and abs(j.lambda_um - i.lambda_um) <= dlam_frac * i.lambda_um]
         if not matches: skip frame i (do NOT return it), and count it in the log
         tmpl, tmpl_var = process.coadd(matches)
         resid = i.data - tmpl ; resid_var = i.var + tmpl_var (if both present, else None)
       Return new Frame objects (ref, lambda_um, pass_idx copied; data=resid; var=resid_var; flags copied).
       Log: n_in, n_out, n_skipped_no_match."""
```
- Why other passes only: a template from the same pass would contain the mover at the same position and subtract it away.
- Expected artefact: a mover leaves a **positive** blob at its current position and **negative** ghosts at its other-pass positions. That's correct and helps the eye.

Tests:
1. A static star whose flux varies with λ (flux = 100·λ) across 3 passes, frames at matching λ → residual at the star < 3σ of the noise in every frame. *Why: proves colour is removed, which the naive median diff fails to do. Assert that the naive `frame - median(all)` leaves > 10σ, to show the contrast.*
2. A star moving 3 px between passes → the residual has a +peak at the current position (> 10σ) and a −peak at the other passes' positions.
3. A frame with no λ match is skipped, and the count is logged.
4. NaN in the template inputs is ignored (output finite where ≥ 1 match has data).

This also becomes a display mode: `mode="lmdiff"` in the API (see the M6 wiring).

## M2. Per-pass co-adds with registration refinement (`pass_stack`)
```python
@dataclass
class PassImage:
    data: np.ndarray      # background-subtracted co-add, NaN where no data
    sigma: float          # robust noise: 1.4826 * MAD of finite pixels
    t_yr: float           # mean of input times
    n: int                # number of frames
    dx: float = 0.0       # registration offset applied (px), filled by refine_registration
    dy: float = 0.0

def pass_stack(frames: list[Frame]) -> list[PassImage]:
    """group_by_pass → coadd per pass → PassImage. Sorted by t_yr. Requires >= 2 passes, else raise ValueError('need >=2 passes')."""
```
Within a pass, frames span days to weeks. Stars move < 0.1 px in that time, so co-adding within a pass is safe.

```python
def find_peaks(img: np.ndarray, sigma: float, nsig: float = config.PEAK_NSIG,
               border: int = 3) -> list[tuple[float, float, float]]:
    """Local maxima (3x3) above nsig*sigma, excluding `border` px from the edge.
    Returns [(x, y, peak_value)] sorted by peak descending. Uses scipy.ndimage.maximum_filter."""

def centroid(img: np.ndarray, x: float, y: float, half: int = 2) -> tuple[float, float, float] | None:
    """Flux-weighted centroid in a (2*half+1)^2 box around round(x), round(y), using only positive pixels.
    Returns (cx, cy, flux) or None if the box is out of bounds, contains NaN, or total flux <= 0."""

def refine_registration(passes: list[PassImage], max_match_px: float = 1.5,
                        min_stars: int = 5) -> list[PassImage]:
    """Relative astrometric correction (field stars define the frame):
    1. Reference = the pass with the most frames. Detect peaks in it (nsig = config.REG_NSIG, a bright subset), take the top 30.
    2. For each other pass: centroid each reference star in that pass (start at the reference position);
       keep those whose |offset| < max_match_px.
    3. If >= min_stars: dx, dy = median offsets. Set PassImage.dx/dy (do NOT resample the image;
       downstream code subtracts dx, dy from measured positions). Else dx = dy = 0 and log a warning.
    Reference pass: dx = dy = 0."""
```
Why: residual WCS errors of ~0.2–0.5 px between passes would fake proper motion of ~1–3″/yr. A median over many field stars removes this. (High-PM stars bias the median only slightly, and the median is robust to them.)

Tests:
5. Shift pass 2 by a known (0.4, −0.3) px using `scipy.ndimage.shift`, with 20 random static stars → recovered dx, dy within 0.05 px.
6. Fewer than 5 stars → dx = dy = 0 and the warning is logged.

## M3. Shift-and-stack motion search (`motion_search`)
```python
@dataclass
class MotionMap:
    snr: np.ndarray        # max over velocities of the stacked SNR, per pixel
    vx: np.ndarray         # argmax vx (px/yr) per pixel
    vy: np.ndarray
    snr_static: np.ndarray # SNR at v = 0
    t0: float
    v_grid: np.ndarray     # 1-D array of the velocity values used per axis

def velocity_grid(passes: list[PassImage], vmax: float = config.VMAX_PX_PER_YR) -> np.ndarray:
    """Step = config.VGRID_STEP_PX / baseline_yr (so the worst-case misalignment across the baseline is <= step px).
    Returns np.arange(-vmax, vmax + step/2, step). Log the grid size."""

def motion_search(passes: list[PassImage], vmax: float = config.VMAX_PX_PER_YR,
                  psf_sigma_px: float = config.PSF_SIGMA_PX) -> MotionMap:
    """
    1. For each pass p: C_p = gaussian_filter(nan_to_num(data), psf_sigma_px) (matched filter), M_p = finite mask
       (also filtered and thresholded > 0.5 so that edges count as missing). w_p = n_p / sigma_p**2.
    2. t0 = sum(w_p t_p)/sum(w_p)
    3. For each (vx, vy) in grid x grid:
         S = sum_p w_p * shift(C_p, (-(vy*(t_p-t0)) - dy_p_correction, -(vx*(t_p-t0)) - dx_p_correction))
             NOTE scipy.ndimage.shift takes (row, col) = (y, x) order! order=1, mode="constant", cval=0
         W = sum_p w_p * shift(M_p, same)     # coverage
         S = S / W where W > 0.5 * sum(w_p) else NaN
         snr_v = (S - nanmedian(S)) / (1.4826 * nanMAD(S))     # empirical noise; robust to model errors
         update the running max (snr, vx, vy) where snr_v > snr
       Registration correction: use the measured offsets. A source at true position x has measured x + dx_p in pass p,
       so shift by -(v*(t_p - t0)) - dx_p. Get this sign right; test 7 checks it.
    4. snr_static = snr_v at v = (0,0).
    Complexity: n_v^2 * n_pass shifts of npix^2. At 0.1° with 3 passes and ~25^2 velocities, that's < 0.5 s. Log the elapsed time.
    """
```
Tests:
7. **Recovery:** 3 passes at t = 2025.4, 2025.9, 2026.4, and a source at SNR 8 per pass moving (vx, vy) = (1.6, −0.4) px/yr, plus 30 static stars → the peak of the snr map is within 1 px of the source position at t0, and the argmax v is within one grid step of the truth. *Why: the core claim, "the tool finds moving objects".*
8. **Faint mover:** per-pass SNR 3.5 (below `PEAK_NSIG` in any single pass) → stacked SNR ≥ 5 at the true v. *Why: this is the reason to use shift-and-stack instead of blinking by eye.*
9. **Static stars:** their argmax v is (0, 0) ± 1 step.
10. **Registration-aware:** the same as test 7, but pass 2 has a registration offset of (0.5, 0) px, with dx set by `refine_registration` → recovered v still within one step. Without the correction (dx forced to 0), it's off by ≥ 1 step. *Why: proves the correction matters and that its sign is right.*

## M4. Candidate extraction + proper-motion fit (`measure_movers`)
```python
@dataclass
class Mover:
    x0: float; y0: float          # position at t0, grid px (FITS orientation)
    ra0: float; dec0: float       # sky position at t0 (via grid.wcs().pixel_to_world_values)
    t0_yr: float
    vx: float; vy: float          # px/yr, from the LSQ fit
    evx: float; evy: float
    pmra_masyr: float; pmdec_masyr: float; epmra_masyr: float; epmdec_masyr: float
    pm_total_arcsec_yr: float
    snr_stack: float
    motion_sig: float             # |v| / sigma_v
    n_pass_used: int
    track: list[tuple[float, float, float]]   # [(t_yr, x, y)] measured centroids, registration-corrected

def measure_movers(passes: list[PassImage], mmap: MotionMap, grid: Grid,
                   nsig: float = config.PEAK_NSIG, min_motion_sig: float = config.MIN_MOTION_SIG,
                   min_v_px: float = config.MIN_V_PX_PER_YR) -> list[Mover]:
    """
    1. Peaks in mmap.snr above nsig (find_peaks with sigma=1, since the snr map is already normalised).
    2. For each peak with |v_argmax| >= min_v_px:
         for each pass p: predict (x0 + vx*(t_p-t0) + dx_p, y0 + vy*(t_p-t0) + dy_p); centroid in the
         RAW pass image (half=2); subtract dx_p, dy_p; keep it if not None.
         centroid error per pass: e = psf_fwhm_px / (2.355 * max(snr_p, 1)), snr_p = flux / (sigma_p * sqrt(25)).
         If fewer than 2 pass centroids: use the grid v and set evx = evy = grid step, motion_sig from the grid.
         Else: weighted linear least squares x(t) = x0 + vx (t - t0), same for y (np.polyfit with w=1/e).
         Error: for exactly 2 points, propagate from e. For >= 3, use the covariance from polyfit(cov="unscaled") * max(1, chi2_red).
    3. Keep it if motion_sig >= min_motion_sig. Convert to the sky (see Conventions).
    4. Deduplicate: peaks within 2 px → keep the higher snr.
    5. Sort by snr_stack descending. Cap at config.MAX_MOVERS.
    """
```
Tests:
11. A synthetic mover with a known PM (e.g. pmra = −8000, pmdec = +700 mas/yr at 6.2″/px, i.e. vx ≈ +1.29, vy ≈ +0.113 px/yr) → the fitted PM is within 3σ of the truth, and σ is realistic (< 1500 mas/yr at SNR 20). *Why: the validation table in the submission depends on it.*
12. **Sign test:** a source moving EAST (RA increasing) → pmra_masyr > 0. *Why: a sign error is likely and embarrassing.*
13. A static-only field → zero movers.
14. Deterministic: the same inputs give the same output.

## M5. Real-data validation (strong model or human; tomorrow morning)
`scripts/validate_motion.py`: for WISE 0855, Barnard's Star and Eps Ind B (det 6, and det 3 for Barnard's), run frames → pass_stack → refine_registration → motion_search → measure_movers. Pick the mover nearest the target position.
Compare to reference PMs stored in `tests/data/reference_pm.json`. **Fill this from SIMBAD/Gaia DR3 during recon. Don't use the values below without checking.** Approximate magnitudes, for sanity only:
- Barnard's Star: pmra ≈ −0.80″/yr, pmdec ≈ +10.36″/yr (Gaia DR3).
- WISE 0855: total ≈ 8.1″/yr, mostly in −RA (Luhman & Esplin).
- Eps Ind B: ≈ 4.7″/yr total, close to Eps Ind A's (Gaia DR3).

Output `notes/motion_validation.md`:

| target | det | n_pass | measured pmra ± | measured pmdec ± | reference | Δ/σ | grid-search time (ms) |

Also record: the number of false candidates in each field, and the registration offsets per pass.
**Acceptance:** |Δ| < 3σ, or < 1.5″/yr for all three targets. If it fails: check the conventions first (signs, row/col order in `shift`), then registration. Report honestly in the submission whatever the result is.

## M6. Wiring (after M1–M4 pass)
- `backend/sequence.py`: new mode `"lmdiff"` → collect all frames, `group_by_pass`, `lambda_matched_residuals`, emit as C5 frames (diff stretch on the client). Not streamable before all frames arrive. That's fine; precompute covers the demo targets.
- New endpoint `GET /api/motion?ra&dec&size&det` → JSON (contract C11). Cached in L2 as `motion/{stack_key}.json`. Computed from the same frames as mode=all (reuse L1).
- `scripts/precompute.py`: also compute `/api/motion` and `lmdiff` for the targets.
- Frontend (`frontend/js/motion.js`):
  - "Find movers" button → GET /api/motion → for each mover draw an arrow from the track's first to last position (C4 orientation: wire_y = npix-1-y), plus a circle at each pass position, animated with the blink.
  - A table: PM ± error, total ″/yr, SNR, significance. Click a row to centre and highlight it.
  - Label: "motion candidate". Never "discovery".
  - Optional: draw the snr map as a heatmap overlay toggle.

## Config constants to add (`backend/config.py`)
```python
# --- motion (Task 08) ---
LAMBDA_MATCH_FRAC = 0.02      # |Δλ|/λ for template matching (≈ SPHEREx resolving power 40–130; tune in M5)
PSF_SIGMA_PX = 1.0            # [VERIFY in recon R4/R8 from a star profile]
PSF_FWHM_PX = 2.355 * PSF_SIGMA_PX
PEAK_NSIG = 5.0
REG_NSIG = 15.0               # bright stars for registration
VMAX_PX_PER_YR = 4.0          # ≈ 25″/yr at 6.2″/px; faster than any known star except Barnard's-class
VGRID_STEP_PX = 0.5           # max misalignment across the baseline per grid step
MIN_V_PX_PER_YR = 0.3
MIN_MOTION_SIG = 3.0
MAX_MOVERS = 20
```

## Done when
- `pytest -q tests/test_motion.py`: all 14 tests pass (0 skipped).
- `python -c "..."` timing: `motion_search` on a synthetic 3-pass 59×59 stack runs in < 0.5 s, and on 291×291 in < 10 s. Paste the numbers.
- M5 table filled in (strong model / human).

## Do NOT
- Resample images for registration (apply offsets to positions only).
- Use per-pass zscale or any display stretch in these algorithms. They operate on float data.
- Call anything a "discovery" in the UI.
