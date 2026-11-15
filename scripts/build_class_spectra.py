"""Build data/classify/features.csv from collected SPHEREx spectra (spectra.jsonl) and SIMBAD multi-band/astrometry.

    python scripts/build_class_spectra.py

Merges SPHEREx 0.75-5.0 um spectra (`data/classify/spectra.jsonl`, or fetches missing ones from EC2 `/api/spectrum`)
with strictly matched SIMBAD optical/NIR photometry and astrometry (`data/classify/labels_clean.csv` or `labels.csv`),
computing the 104-element hybrid feature vector (`classifier.hybrid_features`).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drift import classifier  # noqa: E402

D = Path(__file__).resolve().parent.parent / "data" / "classify"
LABELS_CLEAN = D / "labels_clean.csv"
LABELS_ALL = D / "labels.csv"
SPECTRA = D / "spectra.jsonl"
OUT = D / "features.csv"


def _is_unresolved_or_clean(row: dict) -> bool:
    """Exclude extended host-dominated Seyfert galaxies (galdim > 0.05 arcmin) from point-source quasar labels."""
    if row.get("cls") == "quasar":
        try:
            gal = float(row.get("galdim") or "")
            if np.isfinite(gal) and gal > 0.05:
                return False
        except ValueError:
            pass
    return True


def main() -> int:
    src_labels = LABELS_CLEAN if LABELS_CLEAN.exists() else LABELS_ALL
    rows = [r for r in csv.DictReader(open(src_labels)) if _is_unresolved_or_clean(r)]
    spec_by_id: dict[str, list[dict]] = {}
    if SPECTRA.exists():
        for line in open(SPECTRA):
            if not line.strip():
                continue
            obj = json.loads(line)
            spec_by_id[obj["id"]] = [dict(lambda_um=p[0], flux=p[1], err=p[2]) for p in obj.get("points", [])]

    out_rows = []
    for r in rows:
        pts = spec_by_id.get(r["id"])
        if pts is None:
            continue
        if classifier.features(pts) is None:
            continue
        xh = classifier.hybrid_features(pts, r)
        if xh is None:
            continue
        out_rows.append(dict(r, **{f"f{i}": float(v) for i, v in enumerate(xh)}))

    if not out_rows:
        print("No usable rows found!", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    from collections import Counter
    print(f"Wrote {len(out_rows)} feature rows to {OUT}: {dict(Counter(r['cls'] for r in out_rows))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
