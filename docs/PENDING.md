# Pending items

Status as of 2026-11-14 22:30 UTC. Code on `main` is at `343b651` (later commits are docs and .gitignore only). The EC2 box runs the same code.

## 1. Server (EC2)

**Current state**
- uvicorn is up on `127.0.0.1:8000`, one worker. Health: `data_version sprint-2026-11-15`, 269,456 D6 index rows.
- The server was started before the D4 and D5 indexes finished. Check that D1, D2, D4 and D5 requests work. If they fail, restart the server.
- The SSH tunnel runs only on the laptop where it was opened. Other machines need their own tunnel.

**To do**
- [ ] After the reparse (item 2), restart uvicorn so it reloads the config and the new data version.
- [ ] Confirm `/api/sequence` works for det 1, 2, 4 and 5 (curl, or the page with `det=`).
- [ ] Confirm the server has no tracebacks in `~/logs/uvicorn.log`.
- [ ] Restart with `/tmp/clean_restart.sh` logic (kill the server's whole worker tree). A plain `kill` leaks ~2.5 GB of orphaned workers per restart. Move the script into `scripts/`.
- [ ] Keep `--workers 1`. The process pools are per-process, and the L2 cache is written from one process only.
- [ ] Decide on the public deploy (Docker Compose + Caddy, `<ip>.sslip.io`). It is planned for the end, after the blink checks pass.

**Known server issues**
- The stored spectra for Barnard's Star, RW Aur, R Dra and WISE 0855 (and eps Ind B) are NOT CACHED. The spectrum requests run live on every visit and take about 30 s cold.
- The R Dra light curve returns "no result" on every detector: fewer than two passes have matching wavelengths.
- The R Dra spectrum fails with "no usable frames" on all six detectors.
- The Gaia validation query (`validate_gaia.py`) returns HTTP 500 from the ESA archive. It is blocked, not retried.
- Cache headers are `max-age=300`, not immutable. Keep it short until the 5-target check passes.

## 2. Reparse (decision needed first)

**Experiment results are in: see `docs/flag_mask_experiments.md`.** Option A changes nothing (only 4% of the flagged pixels are saturated). Option B passes M5 (4/4, fewer false candidates) and restores Barnard's and RW Aur cores. R Dra is NOT a mask problem: its aperture is NaN in the raw delivered data. Recommendation: adopt B, bump the data version, re-precompute; handle R Dra separately.


The flag mask hides the bright cores of Barnard's Star, RW Aur and probably R Dra. In most single frames, 11 of the 12 aperture pixels are masked by TRANSIENT and SUR_ERROR. The raw pixels are finite, so the mask is probably too aggressive.

**Decision (needs approval)**
- Option A (proposed): stop masking bits 0 (TRANSIENT) and 2 (SUR_ERROR) on pixels that also carry OVERFLOW (bit 1) or NONLINEAR (bit 15). Keep the masking everywhere else. Saturated pixels stay visible in the flag overlay and the metadata warning.
- Option B: unmask bits 0 and 2 everywhere. Simpler, but cosmic rays could then create false movers before the sigma-clipped co-add exists.
- Option C: keep the mask and label the bright-target spectra and light curves as unavailable. Honest, but it weakens the demo.

**Steps once Option A or B is approved**
- [ ] Edit `FLAG_BITS_BAD` in `backend/config.py`, with the reason in a comment. Option A needs a small change in `process.py` (the mask must depend on the OVERFLOW and NONLINEAR bits).
- [ ] Add tests: a saturated core keeps its flux; a clean faint pixel with TRANSIENT is still masked.
- [ ] Re-run M5 (`scripts/validate_motion.py`). Gate: all 4 runs still pass, with 0 false candidates at D6.
- [ ] Bump `index.data_version()` so the L2 cache is rebuilt.
- [ ] Re-run precompute for all 14 targets, detectors 1–6, modes pass and all, plus extras.
- [ ] Check that the Barnard's, RW Aur and R Dra single frames now show a core, and that the spectra cache.
- [ ] Commit the change, push to GitHub, deploy to EC2 (`git push ec2 main`), restart uvicorn.

**Resolved 2026-11-14:** option B applied. The Explanatory Supplement (Table 9, p.26) was read: TRANSIENT also marks bloom from bright stars; TRANSIENT pixels get an estimated nonlinearity correction and SUR_ERROR pixels none, so unmasked fluxes are less accurate (likely biased low). Spectrum points from such pixels are drawn hollow. R Dra now uses PSF-wing ring photometry. See `docs/flag_mask_experiments.md`.

## 3. Other open items

- [ ] R Dra pass grouping. It is 0.2° from the north ecliptic pole, where SPHEREx observes almost continuously. The 30-day gap rule merges most of a year into one or two "passes". Decide between fixed time bins for continuous fields and keeping the current rule, which needs an explanation in the submission.
- [ ] Light-curve χ² is biased high (about 1.3 for a constant star). Document that values up to about 3 are not variable.
- [ ] Motion error bars are optimistic by about 1.7× (the undersampled PSF). State this in the submission.
- [ ] Preview labels show "inputs so far", not "n of N". The wire format has no total. Add a total field to C5 if wanted.
- [ ] No URL parameter for the per-pass co-add toggle in the band view.
- [ ] Spectrum flux is an aperture sum (MJy/sr × px). Label it that way, or convert it to a physical flux.
- [ ] A spectrum of a moving target requested without `pmra`/`pmdec` returns a garbage spectrum with no warning.
- [ ] Cold 6-detector spectrum load has not been timed on real data.
- [ ] Front-end panels (previews, flags, band filter, spectrum) were tested only with fixtures and a mock server. Check them against the live backend in the browser.
- [ ] Safari, Firefox and phone layout are not tested.

## 4. Submission and sharing

- [ ] Check the Space Apps submission form requirements (video length, slides, repo link, demo URL). SPRINT.md says to do this on the first day. It is still not done.
- [ ] Repo: `github.com/Sankalp-coder-dev/nasa` (private). Make the repo public only after removing `notes/ec2_ids.env` and the EC2 IP and key path from `docs/`.
- [ ] Copy the footprint index to the other computer with rsync (`data/` is not in git).
- [ ] Do not commit `*.pem` or `.env` files.

## Next session starting point

1. `ssh -i ~/.ssh/spherex-blink.pem ubuntu@3.237.70.131 'curl -s 127.0.0.1:8000/api/health'`
2. Read `docs/PENDING.md` (this file) and `docs/work_summary.md`.
3. Start with the decision in section 2. Everything else waits on it.
