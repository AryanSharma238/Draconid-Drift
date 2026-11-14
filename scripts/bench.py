"""Latency budget checker for /api/sequence (Task 04 A6, plan.md section 3).

python scripts/bench.py --base http://localhost:8000 --cold    # deletes local L2+L1 files first (same machine only)
python scripts/bench.py --base http://localhost:8000           # cached budgets

Per target x size it measures: TTFB, time to the `header` line, to the first `frame` line, to `done`, bytes on
the wire. Prints a table with PASS/FAIL per budget; exit code 1 if anything fails or errors.
"""

import argparse
import sys
import time
from pathlib import Path

import httpx
import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets import TARGETS

from backend import cache, config, index
from backend.models import make_grid

SIZES = (0.1, 0.5)
# (first_frame_s, done_s or None) per scenario; plan.md section 3 budgets
BUDGET_CACHED = (1.0, None)
BUDGET_COLD = {0.1: (3.0, 15.0), 0.5: (5.0, 60.0)}
REQUEST_TIMEOUT_S = 300.0


def purge_local(ra: float, dec: float, size: float, det: int, mode: str) -> int:
    """Delete the L2 file and the L1 cutouts for this request. Returns the number of files removed."""
    grid = make_grid(ra, dec, size)
    victims = []
    l2 = cache.l2_get(cache.stack_key(grid, det, mode, index.data_version()))
    if l2:
        victims.append(l2)
    victims += config.RAW_CACHE_DIR.glob(f"*_D{det}_{grid.ra:.3f}_{grid.dec:+.3f}_{grid.size_deg}.fits")
    for p in victims:
        p.unlink()
    return len(victims)


def measure(base: str, ra: float, dec: float, size: float, det: int, mode: str) -> dict:
    t0 = time.perf_counter()
    res = {
        "ttfb": None,
        "header": None,
        "first_frame": None,
        "done": None,
        "bytes": 0,
        "frames": 0,
        "error": None,
    }
    params = {"ra": ra, "dec": dec, "size": size, "det": det, "mode": mode}
    try:
        with httpx.stream("GET", f"{base}/api/sequence", params=params, timeout=REQUEST_TIMEOUT_S) as r:
            res["ttfb"] = time.perf_counter() - t0
            if r.status_code != 200:
                res["error"] = f"HTTP {r.status_code}: {r.read()[:200]!r}"
                return res
            for line in r.iter_lines():
                if not line:
                    continue
                t = time.perf_counter() - t0
                kind = orjson.loads(line)["type"]
                if kind == "header":
                    res["header"] = t
                elif kind == "frame":
                    res["frames"] += 1
                    if res["first_frame"] is None:
                        res["first_frame"] = t
                elif kind == "done":
                    res["done"] = t
            res["bytes"] = r.num_bytes_downloaded
    except httpx.HTTPError as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def verdict(res: dict, size: float, cold: bool) -> tuple[bool, str]:
    if res["error"]:
        return False, "FAIL (error)"
    if res["first_frame"] is None or res["done"] is None:
        return False, "FAIL (no frames)" if res["first_frame"] is None else "FAIL (no done)"
    first_b, done_b = BUDGET_COLD[size] if cold else BUDGET_CACHED
    bad = []
    if res["first_frame"] > first_b:
        bad.append(f"first>{first_b}s")
    if done_b is not None and res["done"] > done_b:
        bad.append(f"done>{done_b}s")
    return (not bad), ("PASS" if not bad else "FAIL (" + ", ".join(bad) + ")")


def fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument(
        "--cold", action="store_true", help="delete local L2/L1 files first and use the cold budgets"
    )
    ap.add_argument("--det", type=int, default=6)
    ap.add_argument("--mode", default="all", choices=("all", "pass"))
    ap.add_argument("--targets", default="all", help="'all' or comma list of scripts/targets.py keys")
    ap.add_argument("--sizes", default=",".join(map(str, SIZES)))
    a = ap.parse_args()
    names = list(TARGETS) if a.targets == "all" else a.targets.split(",")
    sizes = [float(s) for s in a.sizes.split(",")]
    unknown = [n for n in names if n not in TARGETS] + [s for s in sizes if s not in config.FOV_CHOICES_DEG]
    if unknown:
        print(f"unknown targets/sizes: {unknown}", file=sys.stderr)
        return 2
    if a.cold and a.mode == "pass":
        print("note: pass-mode budgets are not in plan.md; using the cold-all budgets", file=sys.stderr)

    cols = ("target", "size", "ttfb_s", "header_s", "first_frame_s", "done_s", "frames", "MB", "verdict")
    rows, failed = [], False
    for name in names:
        ra, dec = TARGETS[name]
        for size in sizes:
            if a.cold:
                purge_local(ra, dec, size, a.det, a.mode)
            res = measure(a.base, ra, dec, size, a.det, a.mode)
            ok, v = verdict(res, size, a.cold)
            failed |= not ok
            rows.append(
                (name, size, fmt(res["ttfb"]), fmt(res["header"]), fmt(res["first_frame"]), fmt(res["done"]),
                 res["frames"], f"{res['bytes'] / 1e6:.2f}", v + (f" {res['error']}" if res["error"] else ""))
            )  # fmt: skip
            print(*rows[-1], flush=True)
    print()
    widths = [max(len(str(r[i])) for r in [cols, *rows]) for i in range(len(cols))]
    for r in [cols, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    print(f"\n{'cold' if a.cold else 'cached'} budgets: {'FAIL' if failed else 'ALL PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
