import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from backend import index
from tests.fixtures import make_fake_cutout

_spec = importlib.util.spec_from_file_location(
    "build_s3_index", Path(__file__).resolve().parent.parent / "scripts" / "build_s3_index.py"
)
bsi = importlib.util.module_from_spec(_spec)
sys.modules["build_s3_index"] = bsi
_spec.loader.exec_module(bsi)

KEY = "dr1/level2/2025W17_4B/l2b-v27-2026-196/6/level2_2025W17_4B_0008_3D6_spx_l2b-v27-2026-196.fits"


def _header_bytes(ra=100.0, dec=20.0, extra_data=True):
    hdul = make_fake_cutout(ra, dec, npix=64)
    hdr = hdul["IMAGE"].header
    hdr["MJD-BEG"] = 60789.5
    hdr["MJD-AVG"] = 60789.6
    prim = fits.PrimaryHDU().header.tostring()
    img = hdr.copy()
    raw = (prim + img.tostring()).encode("ascii")
    return raw + (bytes(range(255, -1, -1)) * 20 if extra_data else b"")  # non-ascii "pixels" after END


def test_footprint_row_ignores_bytes_after_end_and_gives_polygon():
    """The ranged read cuts into pixel data; only the header may be parsed. Corners must be ordered."""
    row = bsi.footprint_row(KEY, _header_bytes())
    assert row["mjd_beg"] == 60789.5 and row["mjd_avg"] == 60789.6
    assert row["exposure_id"] == "2025W17_4B_0008_3" and row["release"] == "dr1"
    ra = np.array([row[f"ra{i}"] for i in range(4)])
    dec = np.array([row[f"dec{i}"] for i in range(4)])
    assert index.contains(100.0, 20.0, ra[None], dec[None])[0]
    assert not index.contains(110.0, 20.0, ra[None], dec[None])[0]


def test_footprint_row_truncated_header_fails_loud():
    """A short read must be a counted failure, not a garbage footprint."""
    with pytest.raises(ValueError, match="END"):
        bsi.footprint_row(KEY, _header_bytes(extra_data=False)[:5000])


def test_writer_resume_reads_back(tmp_path):
    """Resumability: rows written (and flushed) before a kill must be readable on the next run."""
    p = tmp_path / "i.csv.gz"
    row = bsi.footprint_row(KEY, _header_bytes())
    w = bsi._Writer(p)
    w.write(row)
    w.flush()  # simulate kill: file never closed
    assert [r["key"] for r in index.read_rows(p)] == [KEY]
    w.close()
    w2 = bsi._Writer(p)
    w2.write({**row, "key": KEY.replace("0008", "0009")})
    w2.close()
    assert len(index.read_rows(p)) == 2
