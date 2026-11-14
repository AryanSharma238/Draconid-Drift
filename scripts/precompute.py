"""Download real SPHEREx cutouts for the five NASA test targets (and the detector you pick)
and save the cleaned cubes in data/cache/, so the app opens instantly and still works if
IRSA is slow during a demo.

Run on a machine with normal internet access:
    python scripts/precompute.py            # recommended detector per target
    python scripts/precompute.py --all      # every detector (6x the downloads)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift import irsa, pipeline  # noqa: E402
from drift.targets import PRESETS  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--all", action="store_true")
args = ap.parse_args()

for t in PRESETS.values():
    dets = irsa.DETECTORS if args.all else [t.detector]
    for d in dets:
        spec = pipeline.spec_from_target(t)
        spec.detector = d
        try:
            cube, info = pipeline.load_frames(spec, use_cache=False,
                                              progress=lambda a, b: print(f"\r{t.name} {d}: {a}/{b}", end=""))
            pipeline.save_cube(pipeline.cache_path(spec), cube)
            print(f"\n  saved {cube.n} frames for {t.name} {d} ({info['planes']})")
        except Exception as e:
            print(f"\n  FAILED {t.name} {d}: {e}")
