"""E5: validate the motion pipeline against ~150 Gaia DR3 high-PM stars (Task 09 E5).

    python scripts/validate_gaia.py                 # truth (cached) -> run all fields -> summary + plots
    python scripts/validate_gaia.py --truth-only    # just build data/validation/truth.csv
    python scripts/validate_gaia.py --summary-only  # rebuild notes/validation_summary.md + plots from results.csv
    python scripts/validate_gaia.py --limit 5       # smoke test on 5 fields

Resumable: results.csv is appended one row per field as soon as it finishes. On restart, fields with an
`ok` row are skipped; fields with an `error` row are retried.

Only `irsa_measure_field` talks to IRSA (and `backend.catalogs` to Gaia). Everything else is pure and
unit-tested with fakes (tests/test_validate_gaia.py). `irsa_measure_field` has NOT been run end to end
(IRSA was down when this was written); its imports follow SPRINT S1/S2 module names.
"""

import argparse
import asyncio
import csv
import logging
import math
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import itertools

from backend import config
from backend.models import Grid, make_grid
from backend.resolve import propagate

log = logging.getLogger("validate_gaia")

# Constants for this script. They belong in backend/config.py (contract C1); that file was off-limits
# to Track E, so they live here until the merge.
PM_MIN_MASYR = 1500.0
G_MIN = 8.0  # [tune the bright limit after recon shows the saturation magnitude]
G_MAX = 17.0
N_FIELDS = 150
MAX_PER_CELL = 3
HEALPIX_NSIDE = 4
SAMPLE_SEED = 20261114
MATCH_RADIUS_PX = 3.0  # truth <-> mover
FALSE_MATCH_PX = 2.0  # mover <-> any Gaia source (propagated)
CONE_G_MAX = 19.0
CONE_MARGIN_DEG = 0.05  # fast stars move up to ~0.03 deg between Gaia 2016.0 and SPHEREx
FIELD_CONCURRENCY = 4
SIGMA_OK = 3.0  # |dPM|/sigma threshold for "consistent"

VALIDATION_DIR = config.DATA_DIR / "validation"
TRUTH_CSV = VALIDATION_DIR / "truth.csv"
RESULTS_CSV = VALIDATION_DIR / "results.csv"
NOTES_DIR = config.ROOT / "notes"
SUMMARY_MD = NOTES_DIR / "validation_summary.md"

TRUTH_COLS = ["source_id", "ra", "dec", "pmra", "pmdec", "phot_g_mean_mag", "ref_epoch"]
RESULT_COLS = [
    "source_id", "ra", "dec", "pmra", "pmdec", "g", "pm_total", "status", "error",
    "found", "n_movers", "n_false", "mpmra", "mpmdec", "epmra", "epmdec",
    "dpmra", "dpmdec", "sig_ra", "sig_dec", "sig", "dist_px",
]  # fmt: skip


# ----------------------------------------------------------------------------- pure helpers
def pm_total(row: dict) -> float:
    return math.hypot(row["pmra"], row["pmdec"])


def sample_spread(
    rows: list[dict],
    n: int = N_FIELDS,
    per_cell: int = MAX_PER_CELL,
    nside: int = HEALPIX_NSIDE,
    seed: int = SAMPLE_SEED,
) -> list[dict]:
    """Random subset of `rows`, at most `per_cell` per HEALPix cell, at most `n` total. Deterministic."""
    import astropy.units as u
    from astropy_healpix import HEALPix

    hp = HEALPix(nside=nside, order="nested")
    cells = hp.lonlat_to_healpix(
        np.array([r["ra"] for r in rows]) * u.deg, np.array([r["dec"] for r in rows]) * u.deg
    )
    order = np.random.default_rng(seed).permutation(len(rows))
    count: dict[int, int] = {}
    out: list[dict] = []
    for i in order:
        c = int(cells[i])
        if count.get(c, 0) >= per_cell:
            continue
        count[c] = count.get(c, 0) + 1
        out.append(rows[i])
        if len(out) == n:
            break
    return out


def pos_at(row: dict, epoch: float) -> tuple[float, float]:
    """Gaia row (position at its ref_epoch, PM in mas/yr; NaN PM -> 0) propagated to `epoch`."""
    pmra = row["pmra"] if math.isfinite(row["pmra"]) else 0.0
    pmdec = row["pmdec"] if math.isfinite(row["pmdec"]) else 0.0
    return propagate(row["ra"], row["dec"], pmra, pmdec, row["ref_epoch"], epoch)


def _pix(grid: Grid, ra: float, dec: float) -> tuple[float, float]:
    x, y = grid.wcs().world_to_pixel_values(ra, dec)
    return float(x), float(y)


def match_mover(truth: dict, movers: list, grid: Grid, radius_px: float = MATCH_RADIUS_PX):
    """Mover nearest to the truth star's position at the mover's t0 (within radius_px), else None.
    Returns (mover, distance_px) or (None, nan)."""
    best, best_d = None, math.inf
    for m in movers:
        tx, ty = _pix(grid, *pos_at(truth, m.t0_yr))
        d = math.hypot(m.x0 - tx, m.y0 - ty)
        if d < best_d:
            best, best_d = m, d
    if best is None or best_d > radius_px:
        return None, float("nan")
    return best, best_d


def count_false(
    movers: list, matched, gaia_rows: list[dict], grid: Grid, radius_px: float = FALSE_MATCH_PX
) -> int:
    """Movers (other than `matched`) with no Gaia source within radius_px at the mover's t0."""
    n = 0
    for m in movers:
        if m is matched:
            continue
        near = False
        for g in gaia_rows:
            gx, gy = _pix(grid, *pos_at(g, m.t0_yr))
            if math.hypot(m.x0 - gx, m.y0 - gy) <= radius_px:
                near = True
                break
        n += not near
    return n


def build_row(truth: dict, movers: list, grid: Grid, gaia_rows: list[dict]) -> dict:
    """One results.csv row (status ok) from the truth star and the field's measured movers."""
    m, dist = match_mover(truth, movers, grid)
    row = {
        "source_id": truth["source_id"], "ra": truth["ra"], "dec": truth["dec"],
        "pmra": truth["pmra"], "pmdec": truth["pmdec"], "g": truth["phot_g_mean_mag"],
        "pm_total": pm_total(truth), "status": "ok", "error": "",
        "found": int(m is not None), "n_movers": len(movers),
        "n_false": count_false(movers, m, gaia_rows, grid), "dist_px": dist,
    }  # fmt: skip
    if m is not None:
        dra, ddec = m.pmra_masyr - truth["pmra"], m.pmdec_masyr - truth["pmdec"]
        sra, sdec = abs(dra) / m.epmra_masyr, abs(ddec) / m.epmdec_masyr
        row.update(
            mpmra=m.pmra_masyr, mpmdec=m.pmdec_masyr, epmra=m.epmra_masyr, epmdec=m.epmdec_masyr,
            dpmra=dra, dpmdec=ddec, sig_ra=sra, sig_dec=sdec, sig=max(sra, sdec),
        )  # fmt: skip
    return row


def error_row(truth: dict, err: str) -> dict:
    return {
        "source_id": truth["source_id"], "ra": truth["ra"], "dec": truth["dec"],
        "pmra": truth["pmra"], "pmdec": truth["pmdec"], "g": truth["phot_g_mean_mag"],
        "pm_total": pm_total(truth), "status": "error", "error": err,
    }  # fmt: skip


# ----------------------------------------------------------------------------- csv / resume
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_truth(path: Path = TRUTH_CSV) -> list[dict]:
    return [
        {**r, "source_id": int(r["source_id"]), **{k: float(r[k]) for k in TRUTH_COLS[1:]}}
        for r in read_csv(path)
    ]


def save_truth(rows: list[dict], path: Path = TRUTH_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRUTH_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def done_ids(path: Path = RESULTS_CSV) -> set[str]:
    """source_ids with an `ok` row. Error rows are NOT done: they are retried."""
    return {r["source_id"] for r in read_csv(path) if r["status"] == "ok"}


def append_result(row: dict, path: Path = RESULTS_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_COLS, restval="")
        if new:
            w.writeheader()
        w.writerow(row)


def latest_results(path: Path = RESULTS_CSV) -> list[dict]:
    """Last row per source_id (a retried error is superseded by its later ok row)."""
    last: dict[str, dict] = {}
    for r in read_csv(path):
        last[r["source_id"]] = r
    return list(last.values())


# ----------------------------------------------------------------------------- field loop
MeasureFn = Callable[[float, float, float, int], Awaitable[tuple[list, Grid]]]
ConeFn = Callable[[float, float, float], list[dict]]


async def run_fields(
    truths: list[dict],
    measure: MeasureFn,
    cone: ConeFn,
    results_path: Path = RESULTS_CSV,
    size_deg: float = config.DEFAULT_FOV_DEG,
    det: int = config.VALIDATION_DET,
    concurrency: int = FIELD_CONCURRENCY,
) -> dict[str, int]:
    """Process every truth star without an ok row. Each result is appended as it finishes.
    Failures are logged with the source_id, recorded as error rows, and counted. Returns counts."""
    done = done_ids(results_path)
    todo = [t for t in truths if str(t["source_id"]) not in done]
    counts = {"skipped": len(truths) - len(todo), "ok": 0, "error": 0}
    sem = asyncio.Semaphore(concurrency)
    radius = size_deg / 2 * math.sqrt(2) + CONE_MARGIN_DEG

    async def one(t: dict) -> None:
        async with sem:
            sid = t["source_id"]
            try:
                ra, dec = pos_at(t, config.SPHEREX_MID_EPOCH)
                movers, grid = await measure(ra, dec, size_deg, det)
                gaia_rows = await asyncio.to_thread(cone, ra, dec, radius)
                row = build_row(t, movers, grid, gaia_rows)
                counts["ok"] += 1
            except Exception as e:
                log.exception("field failed: Gaia %s (ra=%.5f dec=%.5f)", sid, t["ra"], t["dec"])
                row = error_row(t, f"{type(e).__name__}: {e}")
                counts["error"] += 1
            append_result(row, results_path)

    await asyncio.gather(*(one(t) for t in todo))
    return counts


# ----------------------------------------------------------------------------- IRSA-dependent part
async def irsa_measure_field(ra: float, dec: float, size_deg: float, det: int) -> tuple[list, Grid]:
    """THE ONLY IRSA-DEPENDENT FUNCTION. find_images -> get_frames -> pass_stack -> refine_registration
    -> motion_search -> measure_movers. Also drains sequence_stream so the full stack lands in L2.
    UNTESTED end to end (IRSA down on 2026-11-14); modules are imported lazily so unit tests need none.
    """
    from backend.fetch import get_frames

    from backend import index, motion

    grid = make_grid(ra, dec, size_deg)
    refs = await index.find_images(grid.ra, grid.dec, det)
    if not refs:
        raise RuntimeError(f"no SPHEREx images for ({ra:.5f}, {dec:.5f}) det {det}")
    frames, failed = [], 0
    async for i, frame, err in get_frames(refs, grid):
        if frame is None:
            failed += 1
            log.warning("frame failed: %s (%s)", refs[i].obs_id, err)
        else:
            frames.append(frame)
    if not frames:
        raise RuntimeError(f"all {failed} frames failed for ({ra:.5f}, {dec:.5f})")
    frames.sort(key=lambda f: f.ref.t_mjd)
    passes = motion.refine_registration(motion.pass_stack(frames))
    movers = motion.measure_movers(passes, motion.motion_search(passes), grid)
    await _fill_l2(ra, dec, size_deg, det)
    return movers, grid


async def _fill_l2(ra: float, dec: float, size_deg: float, det: int) -> None:
    """Drain sequence_stream for mode pass + all (hits the L1 cache, fills L2). Failure is logged, not fatal."""
    from backend.sequence import sequence_stream

    from backend.timing import Timer

    for mode in ("pass", "all"):
        try:
            async for _ in sequence_stream(ra, dec, size_deg, det, mode, Timer()):
                pass
        except Exception:
            log.exception("L2 fill failed (%.5f, %.5f det %d mode %s)", ra, dec, det, mode)


def gaia_cone(ra: float, dec: float, radius_deg: float) -> list[dict]:
    from backend import catalogs

    return catalogs.query_cone(ra, dec, radius_deg, CONE_G_MAX)


def build_truth() -> list[dict]:
    from backend import catalogs

    rows = catalogs.query_truth(PM_MIN_MASYR, G_MIN, G_MAX)
    rows = [r for r in rows if all(math.isfinite(r[k]) for k in ("ra", "dec", "pmra", "pmdec"))]
    log.info("Gaia truth candidates: %d", len(rows))
    return sample_spread(rows)


# ----------------------------------------------------------------------------- summary
def _f(r: dict, k: str) -> float:
    return float(r[k]) if r.get(k) not in (None, "") else float("nan")


def _bin_table(rows: list[dict], key: str, edges: list[float], label: str) -> list[str]:
    out = [f"| {label} | fields | recovered | fraction |", "|---|---|---|---|"]
    for lo, hi in itertools.pairwise(edges):
        sel = [r for r in rows if lo <= _f(r, key) < hi]
        k = sum(int(r["found"]) for r in sel)
        out.append(
            f"| {lo:g} to {hi:g} | {len(sel)} | {k} | {k / len(sel):.2f} |"
            if sel
            else f"| {lo:g} to {hi:g} | 0 | 0 | n/a |"
        )
    return out


def summarize(results: list[dict]) -> str:
    ok = [r for r in results if r["status"] == "ok"]
    n_err = len(results) - len(ok)
    lines = ["# Gaia validation summary", ""]
    if not ok:
        return "\n".join(lines + [f"No successful fields ({n_err} errors)."]) + "\n"
    found = [r for r in ok if int(r["found"])]
    lines += [
        f"- Fields attempted: {len(results)} ({len(ok)} ok, {n_err} errored; errored fields are excluded below)",
        f"- Recovery (truth star matched within {MATCH_RADIUS_PX:g} px): {len(found)}/{len(ok)} = {len(found) / len(ok):.2f}",
    ]
    if found:
        dpm = [math.hypot(_f(r, "dpmra"), _f(r, "dpmdec")) for r in found]
        consistent = sum(_f(r, "sig") < SIGMA_OK for r in found)
        lines += [
            f"- Median |dPM| (found): {np.median(dpm):.0f} mas/yr",
            f"- |dPM|/sigma < {SIGMA_OK:g} (both components): {consistent}/{len(found)} = {consistent / len(found):.2f}",
        ]
    nf = np.array([int(r["n_false"]) for r in ok])
    lines += [
        f"- False movers per field: median {np.median(nf):.1f}, 90th percentile {np.percentile(nf, 90):.1f}",
        "",
        "## Recovery by Gaia G magnitude",
        *_bin_table(ok, "g", [8, 12, 14, 16, 17.0001], "G"),
        "",
        "## Recovery by total proper motion (mas/yr)",
        *_bin_table(ok, "pm_total", [1500, 2500, 4000, 1e9], "PM"),
        "",
    ]
    return "\n".join(lines)


def make_plots(results: list[dict], out_dir: Path = NOTES_DIR) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ok = [r for r in results if r["status"] == "ok"]
    found = [r for r in ok if int(r["found"])]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    fig, axs = plt.subplots(1, 2, figsize=(10, 4.5))
    for ax, (tk, mk, ek, name) in zip(
        axs, [("pmra", "mpmra", "epmra", "pmra"), ("pmdec", "mpmdec", "epmdec", "pmdec")], strict=True
    ):
        t = [_f(r, tk) for r in found]
        ax.errorbar(t, [_f(r, mk) for r in found], yerr=[_f(r, ek) for r in found], fmt="o", ms=3)
        lim = (min(t + [0]) * 1.05, max(t + [0]) * 1.05) if t else (-1, 1)
        ax.plot(lim, lim, "k--", lw=1)
        ax.set_xlabel(f"Gaia {name} (mas/yr)")
        ax.set_ylabel(f"measured {name} (mas/yr)")
    p = out_dir / "validation_pm.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    paths.append(p)

    fig, ax = plt.subplots(figsize=(5.5, 4))
    edges = [8, 10, 12, 14, 15, 16, 17.0001]
    mids, frac = [], []
    for lo, hi in itertools.pairwise(edges):
        sel = [r for r in ok if lo <= _f(r, "g") < hi]
        if sel:
            mids.append((lo + min(hi, 17)) / 2)
            frac.append(sum(int(r["found"]) for r in sel) / len(sel))
    ax.plot(mids, frac, "o-")
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Gaia G (mag)")
    ax.set_ylabel("recovery fraction")
    p = out_dir / "validation_recovery_vs_g.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    paths.append(p)
    return paths


def write_summary() -> None:
    results = latest_results()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_MD.write_text(summarize(results))
    paths = make_plots(results)
    print(f"wrote {SUMMARY_MD} and {', '.join(map(str, paths))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth-only", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="only the first N truth stars (smoke test)")
    ap.add_argument("--concurrency", type=int, default=FIELD_CONCURRENCY)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.summary_only:
        write_summary()
        return
    if not TRUTH_CSV.exists():
        save_truth(build_truth())
        print(f"wrote {TRUTH_CSV}")
    if args.truth_only:
        return
    truths = load_truth()[: args.limit]
    counts = asyncio.run(run_fields(truths, irsa_measure_field, gaia_cone, concurrency=args.concurrency))
    print(f"fields: {counts}")
    write_summary()


if __name__ == "__main__":
    main()
