# Draconid Drift

Planet X and SPHEREx, NASA Space Apps 2026. Enter RA/Dec or click an all-sky map, then watch the SPHEREx frames of that patch as a short smooth video and get a one-number verdict on whether something moved.

## Run

```
pip install -r requirements.txt
streamlit run app.py
```

Deploy: push to GitHub, then create the app on Streamlit Community Cloud with `app.py` as the entry point.

Real data needs internet access to IRSA. If your network blocks it, run `python scripts/precompute.py` once on a machine that can reach IRSA. It caches cutouts for the five presets in `data/cache/`, and the app loads from the cache first.

Tick "demo data" in the app to use labelled synthetic frames.

Tests: `python -m pytest -q`

## The five presets

Barnard's Star, Epsilon Indi B, WISE 0855, RW Aurigae, R Draconis. Positions are moved to the SPHEREx epoch (2026.3) using proper motion before searching.

## Player

Fixed inverted grayscale, one stretch for every frame, same grid, a date and wavelength label on each frame, and an epoch strip. All frames are preloaded so playback does not flicker. Views: all frames, burst stacks, and fixed versus motion-tracked stack.

## The verdict

The big number is the confidence = 1 − FAP. FAP is the false-alarm probability from time-permutation scrambles over the same region and velocity grid, so the look-elsewhere effect is included. It is not the probability that the object is real.

Hover the number to see the five checks:

| Check | Meaning | Pass rule (heuristic) |
|---|---|---|
| Strength ν | matched-filter S/N along the best track | reported |
| Moving vs still Δχ² | track fit versus the best stationary source met on the track | must be positive |
| Split-half | each half of the frames alone | min half ≥ 0.5 ν |
| Leave-one-frame-out | drop each frame in turn | min ≥ 0.8 ν |
| Flagged pixels | fraction of flagged pixels on the track | ≤ 25% |

The thresholds are heuristics, not calibrated cut-offs.

## Honest limits

- Never tested on real SPHEREx data. The sandbox it was built in could not reach IRSA. All tests use synthetic frames.
- The FAP covers a Gaussian-noise null with scrambles, and assumes constant flux over time.
- WISE 0855 may be too faint in SPHEREx. Its listed coordinates are assumed to be epoch 2010.5.
- Flag bit numbers should be checked against the file headers.
- The Aladin click and the "Use this position" hand-off into Streamlit are untested. The SPHEREx HiPS layer is experimental.
- Tracks passing through bright stars are rejected.
- Demo data is always labelled synthetic.
