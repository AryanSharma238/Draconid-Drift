# SPHEREx Blink Tool: work summary

## What the tool is

The tool shows a time-ordered blink of SPHEREx infrared cutouts around a sky position. Users enter a name or RA/Dec and a field of view, then pick a detector and a mode. It supports wavelength-matched differences, motion candidates (shift-and-stack), light curves for variable stars, and spectra built from the six detectors.

It runs on one EC2 instance (`3.237.70.131`, 8 vCPU, us-east-1). It reads public SPHEREx data from the S3 mirror `nasa-irsa-spherex`, because the IRSA archive was down during the sprint.

## How the work was organised

The sprint plan (`tasks/SPRINT.md`) split the work into tracks A to E. Track A (the coordinator) owned contracts, config, review and merges. Tracks B, C and D were built by Sonnet agents, each in its own git worktree and branch. Track E built the identifier, resolver, photometry and Gaia validation code. Every merge was tested before it landed in `main`. The repo is on the EC2 box and on GitHub (`Sankalp-coder-dev/nasa`, private).

## Data processing

1. **Footprint index.** `scripts/build_s3_index.py` lists the D6 keys for DR1 and QR3 and reads only the first 28.8 KB of each file (the FITS headers). It stores the four sky corners and the MJD for each frame. The index has 269,456 rows for D6, and about 270,000 each for D4 and D5. A process pool cut the build from about 75 minutes to about 19.
2. **Detector pairs.** D1 shares pointing with D4, D2 with D5, and D3 with D6. CRVAL agrees to 0.02° on real headers, so one index file covers each pair (`config.S3_INDEX_PAIRS`).
3. **Finding frames.** `find_images` runs an exact point-in-quadrilateral test in a gnomonic projection. It deduplicates by release priority (DR1, then QR3, then QR2), then by the highest version number.
4. **Cutout reads.** A ranged-GET reader pulls only the grid box from each HDU (IMAGE, FLAGS, VARIANCE, ZODI) and the FLAGS tile heap, in about four parallel rounds instead of 19 sequential reads. Its output is byte-identical to the old reader on 12 real frames.
5. **Processing.** Each cutout is reprojected to a common north-up grid, background-subtracted (ZODI and a median), and tagged with its wavelength from the WCS-WAVE HDU. Pixels with configured flag bits are masked.
6. **Pass co-adds.** Frames are grouped into survey passes by a 30-day gap, and each pass is co-added.
7. **Motion search.** Passes are registered, a shift-and-stack search runs over velocities, candidates are extracted, and a proper motion is fitted. Validation against published proper motions (M5) passed for WISE 0855, Barnard's Star and eps Ind B. A bug was found along the way: a speed cut on a coarse velocity estimate discarded sub-PSF movers. The gate now uses the refined velocity.
8. **Light curves.** These use wavelength-matched templates and aperture photometry. A constant star gives a reduced χ² near 1.3, so values up to about 3 should not be read as variability.
9. **Caching.** L1 stores cropped FITS per frame. L2 stores finished stacks as gzip NDJSON, keyed by data version, position, size, detector and mode. Server cache headers are 300 s.
10. **Precompute.** `scripts/precompute.py` fills L2 for 14 targets (the 5 test targets plus 9 extras) across detectors and modes, plus motion, lmdiff, light curves and spectra.

## Server

- `/api/sequence` streams NDJSON frames in modes all, pass and lmdiff. Pass previews arrive early, so all passes show within about 1 s on a cold position.
- A cold random position takes about 2.5 to 3 s for a full blink, down from about 9 s.
- `/api/motion` returns movers, `/api/lightcurve` returns light curves, and `/api/spectrum` returns the six-detector spectrum with the aperture following the target's proper motion.
- `/api/resolve` turns a name into coordinates and proper motion. `/api/health` reports status.
- Process pools and indexes warm up at startup (about 11 s).

## Front end

- Blink player with play, step and speed controls. A crosshair follows proper motion.
- Try links for the 5 test targets, all served from cache.
- Find movers: arrows, pass-position circles, a proper-motion table with errors, and an error message when the search fails.
- λ-matched difference mode with a diverging colour scale.
- Light-curve plot, shown on click.
- Missing data drawn grey with a hatch, not as dark sky.
- Flag overlay, hover readout, and a per-frame metadata line that warns about saturation.
- Wavelength band slider and a per-pass co-add of the band.
- Spectrum panel with hollow saturated points, CSV export and click-anywhere spectra.
- URL parameters round-trip (name, RA/Dec, size, detector, mode, PM, epoch, band).

## Commands

Connect and run the site:

```bash
# 1. Tunnel from your laptop (keep it open), then open http://localhost:8000/
ssh -i ~/.ssh/spherex-blink.pem -N -L 8000:127.0.0.1:8000 ubuntu@3.237.70.131

# 2. Log in to the box
ssh -i ~/.ssh/spherex-blink.pem ubuntu@3.237.70.131
cd ~/nasa && source .venv/bin/activate

# 3. Health check (on the box)
curl -s 127.0.0.1:8000/api/health

# 4. Restart the server
pkill -f "uvicorn backend.app:app"
nohup setsid uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1 \
  > ~/logs/uvicorn.log 2>&1 < /dev/null &
tail -f ~/logs/uvicorn.log   # wait for "warm-up done"
```

Build and fill data (on the box):

```bash
python scripts/build_s3_index.py --det 4 --releases dr1,qr3   # also --det 5 and --det 6
python scripts/validate_motion.py                             # M5 validation
python scripts/precompute.py --targets all --dets 1,2,3,4,5,6 --modes pass,all --size 0.1
```

Tests and deploy (on your laptop):

```bash
source .venv/bin/activate
pytest -q                          # 215 passed, 3 deselected (network tests)
node frontend/test/run_node.mjs    # front-end tests
git push ec2 main                  # deploy to the box
git push origin main               # GitHub
```

## Status

- **Live:** the server runs on the box and the tunnel works. D1 to D6 are indexed, and precompute has cached 566 stacks.
- **Open decision:** the flag mask hides the bright cores of Barnard's Star, RW Aur and probably R Dra in most single frames. Their spectra and light curves therefore don't cache. The proposed fix unmasks TRANSIENT and SUR_ERROR where OVERFLOW or NONLINEAR is also set, with M5 as the acceptance test. Not yet applied; needs approval.
- **Other open items:** the R Dra pass grouping (a pole field merges most of a year into two "passes"); the Gaia validation query returns HTTP 500; the submission form requirements are unchecked; the public deploy is planned for the end.
- **Not verified on real data:** the new panels (previews, flags, band filter, spectrum) were tested with fixtures and a mock server only.
- **Sharing:** `data/` is not in git. The footprint index must be copied with rsync. The SSH key must never go into the repo.
