# SPRINT — submission deadline: end of day 2026-11-15

**This file overrides `plan.md` and the task files wherever they conflict.** The task files remain the detailed reference for the parts kept here. Skip every section this file marks CUT.

> **DECISION 2026-11-14 (IRSA outage): data comes from S3, not IRSA.** IRSA TAP, SIA and the cutout service are all down (502/503). The public mirror `s3://nasa-irsa-spherex` works. This **UN-CUTS** the S3 fetch path (Task 03 Part C `s3` branch, `FETCH_METHOD="s3"`) and replaces S1's TAP query with a local footprint index built from S3 headers (`scripts/build_s3_index.py`, Track B). TAP code stays only as a fallback. `ImageRef.uri` = S3 key (contract C2 change log). Everything else in this file still applies.

## Differentiator (decided 2026-11-14): algorithms, not just a viewer
Most entries will be LLM-built "cutout → PNG → blink" viewers. Ours wins on:
1. **Wavelength-matched differencing** (Task 08 M1). This is correct for SPHEREx's per-pixel λ, where naive diffs show colour instead of motion.
2. **Shift-and-stack motion search** (Task 08 M3). It *finds* movers, including ones too faint to see in any single pass.
3. **Measured proper motions with errors, validated against Gaia/literature** (M4, M5). This goes in the submission as a table.
4. **Fast:** precomputed + streamed + client-side display. Table stakes, but we make it measurable (`bench.py` numbers in the submission).

5. **What others will miss** (Task 09):
   - E1: targets drawn at their *current* positions (Barnard's has moved 4.5′), with a moving crosshair.
   - E2: information-first streaming (pass blink in < 300 ms).
   - E3: robust co-adds + mesh background.
   - E4: λ-matched light curves for the variables.
   - E5: **overnight validation on ~150 Gaia high-PM stars.**
   - E6: precomputed candidate gallery.
   - E7: mover spectrum + CH₄ index.

**Objective: fast + preprocess heavy.** Everything shown in the demo is precomputed. The live path exists for arbitrary positions but is not what judges see first.

**Priority order:** MVP blink (Tasks 03–05 subset) → Task 08 M1–M4 → E1, E3 (tonight) → **E5 overnight run** → Task 08 M5/M6 + E2 + E4 (tomorrow morning) → E6 → F3/E7. Cut from the end.

## What we submit
A public URL where a user enters a name or RA/Dec and sees an aligned blink sequence of SPHEREx images. The tool includes:
- per-pass co-adds per detector, and a difference mode;
- URL params + embed mode;
- the 5 test targets precomputed (instant);
- **stretch goal:** spectrum on click.

Plus whatever the Space Apps submission form requires. **Check the form requirements TODAY** (video length, slides, repo link, demo URL) and don't guess them.

## What is CUT (do not build)
| Cut | Replaced by |
|---|---|
| Task 02 SQLite/HEALPix index | Live TAP + disk cache of query results (S2 below) |
| S3 range reads (recon R7, R9, FETCH_METHOD=s3) | IBE cutout URLs only |
| Task 04b catalogue build / spot batch | `scripts/precompute.py` for the 5 targets + ~10 extras |
| CloudFront, S3 bucket, IAM, EventBridge, CloudWatch (Task 07 D4, D5, D7–D9) | One EC2 + Docker Compose + Caddy (TLS via a free domain or sslip.io) |
| Features F2, F4–F10 | Only diff presets (MVP) + F3 spectrum if time allows |
| Live zscale messages | Server sends zscale in the `done` line; the client uses a provisional zscale from frame 0 until then |
| WiseView parity doc | 15-minute look, 5 bullet points in notes |

Contracts (`docs/contracts.md`) still apply, except: C8 (index) is unused, and in C7 only L1 + L2 are used.

## Parallel execution (file ownership = no merge conflicts)

**Step 0 (serial, ~30 min):** Task 00 setup, which ends in an initial git commit on `main`. Every track depends on its skeleton (config, models, fixtures). Track A can start recon downloads at the same time (its scripts only write under `data/recon/`).

**Then run each track in its own git worktree + branch + Claude Code session:**
```bash
git worktree add ../nasa-B -b track-b   # repeat for C, D, E
cd ../nasa-B && uv venv --python 3.14 .venv && uv pip install -r requirements.txt -r requirements-dev.txt
```

| Track | Model | Starts | Owns (ONLY these files) | Work, in order |
|---|---|---|---|---|
| **A** | Opus / you | now | `scripts/download_target.py`, `inspect_cutout.py`, `spx_util.py`, `targets.py`, `notes/*`, `tests/data/*`, `docs/contracts.md`, **values** in `backend/config.py` | Recon-lite → fill [VERIFY] → review D's signs → integration → deploy → submission |
| **B** | lower | after step 0 | `backend/process.py`, `encode.py`, `fetch.py`, `index.py`, `sequence.py`, `cache.py`, `app.py`, `scripts/precompute.py`, `scripts/bench.py`, matching tests | **First** `coadd` + `group_by_pass` (commit within ~1 h, D needs them) → rest of 03 Part A/B → (after recon) 03 Part C → S1/S2 API → E3 |
| **C** | lower | after step 0 | `frontend/**`, `scripts/make_fixture_stream.py` | Write its own C5 fixture generator (numpy + base64, no backend imports) → Task 05 → E1 frontend (moving crosshair) → M6 frontend (arrows, PM table) → E6 gallery page |
| **D** | lower | after step 0 | `backend/motion.py`, `tests/test_motion.py` | 08 **M3, M4** first (build `PassImage`s directly in tests, no coadd needed) → M2 → M1 (after B's coadd lands) → E4 |
| **E** | lower | after step 0 | `backend/ids.py`, `resolve.py`, `photometry.py`, `catalogs.py`, `scripts/validate_gaia.py`, matching tests | Task 02 I1 (ids) → A1 + E1 backend (resolve + PM propagate) → `aperture_flux` → E5 validation script (ready to launch at ~23:00) |

**Shared-file rules:**
- `backend/config.py`: Task 00 writes **all** constants up front (C1 + Task 08 + Task 09 config sections). After that only Track A changes values. Other tracks never add or edit constants; they ask A.
- `backend/models.py`, `requirements*.txt`, `pyproject.toml`: frozen after step 0. A change needs A's approval and goes into `main` first.
- `docs/contracts.md`: Track A only.
- Cross-track calls go **only** through the signatures in the task files. If you need a function another track owns and it isn't merged yet, stub it in your test. Never write it yourself.

**Merge protocol (Track A runs this at each checkpoint, ~every 2 h):**
1. Each track: `pytest -q` green → commit → report (CLAUDE.md format).
2. A: `git merge track-x` into main in the order B → E → D → C (dependency order), run `pytest -q` on main after each.
3. Each track: `git merge main` (not rebase) before continuing.
- Checkpoints: **T+1 h** (B's coadd), **T+3 h** (recon done → B starts fetch), **evening** (API + frontend integration), **22:30** (everything needed for E5 merged).

**Don't run more than 5 sessions.** Track A's review and merge time is the real bottleneck, not compute.

## Parallel tracks (summary)
The contracts let backend and frontend proceed in parallel. The frontend develops against a **fixture stream file** until the API is up.

- **Track A (you / Opus):** recon-lite → integration → deploy → submission.
- **Track B (lower model sessions):** setup → process/encode → fetch → API → precompute.
- **Track C (lower model session, parallel with B):** frontend against the fixture.

## Schedule (adjust to the actual hours you have)

### Today (2026-11-14)
| Block | Track | Work | Exit check |
|---|---|---|---|
| T1 (1 h) | B | **Task 00 in full.** Python 3.14 + uv | `pytest -q` green |
| T1–T3 (3 h) | A | **Recon-lite:** 01-recon steps R1, R2, R3 (5 targets, size 0.1 only), R4, R6 (filename regex, dedupe, MJD only), **R8 blink check**, R10, R11 | R8 = GO. No [VERIFY] left in config.py, except S3 values (cut) |
| T1 (½ h) | A | Write `tests/data/fixture_stream.ndjson`: a fake 15-frame, 3-pass stream in C5 format, with a moving synthetic star, produced by a 30-line script using `tests/fixtures.make_fake_cutout` + `encode.py` (or hand-write the encode logic if Task 03 Part B isn't done yet). Hand it to Track C | File exists and parses |
| T2–T5 | C | **Task 05 F1–F7**, loading `fixture_stream.ndjson` via `?fixture=1` (fetch the static file instead of `/api/sequence`) | Fixture blinks; star visibly moves; URL round-trips |
| T3–T6 | B | **Task 03 Parts A and B** (process + encode) with all their tests. Then **Part C** (fetch) **without** the s3 branch | `pytest -q` green; `scripts/try_fetch.py` on WISE 0855 works |
| T2–T6 | **D** (lower model) | **Task 08 M1–M4** on synthetic data (pure numpy, no network). Needs Task 00 + Task 03 Part A's `coadd`/`group_by_pass`. If Part A isn't ready, implement against the signatures and stub them in the tests | `pytest tests/test_motion.py`: 14/14 pass |
| Evening | B | **S1–S3 below** (simplified API) | `curl -N` shows a stream for WISE 0855 |
| Evening | A | Review Task 08 sign conventions (tests 7, 10, 12). Fill `tests/data/reference_pm.json` from SIMBAD/Gaia | Reviewed |
| Evening | D | **Task 09 E3** (robust co-add + mesh background), then **E1** backend (`resolve_name_pm`, `propagate`) | Tests pass |
| Evening | C | **Task 09 E1** frontend (moving crosshair, predicted track) | Barnard's under the crosshair (on fixture: a synthetic PM) |
| **~23:00 launch** | A | **Task 09 E5 overnight validation run**, plus `precompute.py` for the targets, both resumable. Laptop or EC2 | Running; check progress before bed |
| **Hard checkpoint tonight** | A | Real WISE 0855 blink in the browser, locally | If this fails, tomorrow morning goes to fixing it. Nothing else |

### Tomorrow (2026-11-15)
| Block | Track | Work | Exit check |
|---|---|---|---|
| Morning (2 h) | A+B | Run the **5-target check** (Task 05 F9 table). Fix alignment/orientation/stretch bugs | All 5 look right; screenshots saved |
| Morning (1 h) | B | `scripts/precompute.py` for the 5 targets + extras, at det 3 and 6, modes pass, all and lmdiff, + `/api/motion` | Cached loads < 1 s locally |
| Morning (2 h) | A | **Task 08 M5: real-data PM validation** (WISE 0855, Barnard's, Eps Ind B) | `notes/motion_validation.md` table filled in |
| Morning (2 h) | B/C | **Task 08 M6 wiring:** lmdiff mode, `/api/motion`, frontend arrows + PM table | "Find movers" works on WISE 0855 |
| Morning (1 h) | A | Read the E5 results → `notes/validation_summary.md` + plots (these go into the submission) | Summary written |
| Morning (2 h) | D | **E2** (pass-first stored stacks) + **E4** (λ-matched light curves for R Dra / RW Aur) | Tests pass; plots render |
| Midday (2 h) | C/D | **E6 gallery** from the E5 fields | Gallery page live |
| Midday (2 h) | A | **Deploy:** Task 07 D1–D3 + D6 only. Copy `data/cache/stacks` to the box. Run `bench.py` against prod | Public URL works from a phone. Embed iframe works |
| Midday (2–3 h) | C | **F3 spectrum** (Task 06), ONLY if Task 08 M6 is done and the 5-target check passed. Otherwise skip it | Spectrum for WISE 0855 renders |
| Afternoon (2 h) | A | **Submission:** a Barnard's Star "others centre on the wrong spot, we track it" moment; the E5 recovery numbers + measured-vs-Gaia plot; R Dra λ-matched light curve; demo of WISE 0855 blink → "Find movers" → arrow + measured PM; the PM validation table; naive diff vs λ-matched diff side by side (R Dra or a busy field); `bench.py` numbers; README explaining the algorithms | Form drafted |
| **Code freeze: 3 h before the deadline** | all | Only bug fixes after this. Submit with ≥ 1 h to spare | Submitted |

## Simplified backend pieces (replace Task 02 and part of Task 04)

### S1. `backend/index.py` (live TAP + cache, no SQLite)
```python
async def find_images(ra: float, dec: float, det: int) -> list[ImageRef]:
    """1. Cache file: DATA_DIR/'tap'/f'{ra:.3f}_{dec:+.3f}_D{det}.json'. If it exists and is < 24 h old → load, return.
       2. Else run the recon R2 ADQL (single detector) via pyvo in asyncio.to_thread.
       3. parse with ids.parse_uri (Task 02 step I1, which IS still needed), then ids.dedupe.
       4. Write the cache atomically; return."""
def data_version() -> str:
    return "sprint-2026-11-15"   # constant; bump manually if you re-run precompute after new data
```
Do Task 02 **step I1 only** (`backend/ids.py` + its tests). Skip I2–I6.

Search radius note: `CONTAINS(POINT(ra,dec), poly)` returns images containing the centre only. For the sprint this is fine; edge-of-footprint frames simply get NaN borders.

### S2. API
Follow Task 04 A1, A2 (L2 only, no `s3_url`), A3, A4 and A6, with these changes:
- `sequence_stream` gets refs from S1 (which costs TAP latency, ~5–20 s cold). Send the `header` line **before** the TAP query finishes? No: n_expected needs the refs. Instead, send an early `{"type":"status","msg":"querying archive"}` line first, so the UI can show progress. The client ignores unknown types except for showing `msg`.
- zscale: compute from all frames at the end and put it in `done` as `"zscale":[lo,hi]`. The stored L2 header gets the final zscale.
- Skip the Server-Timing details. Log stage times only.

### S3. `scripts/precompute.py`
```
python scripts/precompute.py --targets all --dets 3,6 --modes pass,all --size 0.1
```
Drain `sequence_stream` for each combination (this fills L2). Print a table: target, det, mode, frames, failed, seconds. Extras: add ~10 famous objects to `scripts/targets.py` (e.g. Luhman 16, Proxima Cen, Wolf 359, Teegarden's Star, Mira, Betelgeuse, Orion Nebula, M42, Pleiades, Andromeda) [positions resolved with SkyCoord.from_name, PM-propagated where applicable].

## Frontend changes vs Task 05
- Add `?fixture=1` (dev only): load `/test/fixture_stream.ndjson` instead of the API.
- Handle the `status` message (show its text) and `done.zscale`.
- Add a "Try:" row of buttons for the 5 test targets + extras (links with URL params). That makes the demo one click.
- Skip the F8 DevTools perf work unless playback visibly stutters.

## Deploy (Task 07 subset)
- One `t4g.medium` (or `t3.medium` if the ARM wheel check fails), Ubuntu 24.04, 30 GB gp3, in `us-east-1`.
- Security group: 22 from your IP; 80/443 from anywhere.
- Docker Compose (D1, D2) + Caddy (D3) with `DOMAIN=<elastic-ip>.sslip.io` for automatic HTTPS without buying a domain.
- `rsync -a data/cache/ ubuntu@host:/data/cache/` to ship the precomputed stacks.
- `--workers 1` stays.

## Honest limits of the motion algorithms (put these in the submission)
- Only ~3 survey passes so far, so PM fits have 1 degree of freedom, and errors will be ~0.5–2″/yr.
- Parallax is not fitted. That's fine for these targets: the WISE 0855 parallax is 0.44″, < 0.1 px.
- Asteroids and comets (which move within a pass) are not handled. They're out of scope (plan.md §2a).
- Candidates are hints for human inspection, not detections.

## Risks for this timeline (watch these)
0. **Task 08 sign/orientation errors.** Tests 7, 10 and 12 exist to catch them. A strong model reviews them tonight.
1. **R8 blink check fails** (motion not visible). Decide by tonight: fall back to showing variability (R Dra, RW Aur) as the headline, plus difference mode; describe the motion limits honestly in the submission.
2. **IRSA slow or down tomorrow.** Precompute TONIGHT, not tomorrow. The demo then never depends on live IRSA.
3. **Orientation/alignment bugs** found late. The 5-target check is scheduled for tomorrow morning, not the afternoon.
4. **Deploy surprises.** Have a fallback: a recorded demo video plus a local-run README. Deploy before the features.
