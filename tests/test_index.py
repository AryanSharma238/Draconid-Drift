import asyncio
import csv
import gzip

import numpy as np
import pytest

from backend import config, index

VER = "l2b-v27-2026-196"


def _key(release, week, exp, ver, det=6):
    return f"{release}/level2/{week}/{ver}/{det}/level2_{exp}D{det}_spx_{ver}.fits"


def _square(ra, dec, half=1.74, rot=0.0):
    """Corners of a tangent-plane square (deg), rotated by rot degrees."""
    t = np.deg2rad(rot)
    pts = [(-half, -half), (half, -half), (half, half), (-half, half)]
    out = []
    for x, y in pts:
        xi = np.deg2rad(x * np.cos(t) - y * np.sin(t))
        eta = np.deg2rad(x * np.sin(t) + y * np.cos(t))
        d0 = np.deg2rad(dec)
        rho = np.hypot(xi, eta)
        c = np.arctan(rho)
        dd = np.arcsin(np.cos(c) * np.sin(d0) + (eta * np.sin(c) * np.cos(d0) / rho if rho else 0))
        rr = np.deg2rad(ra) + np.arctan2(
            xi * np.sin(c), rho * np.cos(d0) * np.cos(c) - eta * np.sin(d0) * np.sin(c)
        )
        out.append((np.rad2deg(rr) % 360, np.rad2deg(dd)))
    return out


def _row(release, exp, ver, mjd, ra, dec, rot=0.0, week="2025W20_1B"):
    r = {
        "key": _key(release, week, exp, ver),
        "release": release,
        "exposure_id": exp,
        "version": ver,
        "det": 6,
        "mjd_beg": mjd,
        "mjd_avg": mjd + 0.01,
    }
    for i, (a, d) in enumerate(_square(ra, dec, rot=rot)):
        r[f"ra{i}"], r[f"dec{i}"] = a, d
    return r


def _write(path, rows, truncate=False):
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=index.INDEX_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    if truncate:
        path.write_bytes(path.read_bytes()[:-5])


@pytest.fixture
def idx_path(tmp_path, monkeypatch):
    p = tmp_path / "idx.csv.gz"
    monkeypatch.setattr(config, "S3_INDEX_PATH", p)
    monkeypatch.setitem(config.S3_INDEX_PATHS, 6, p)
    index._cache.clear()
    return p


def _find(ra, dec, det=6):
    return asyncio.run(index.find_images(ra, dec, det))


def test_point_in_footprint_exact_including_rotated(idx_path):
    """Edge-of-footprint decisions must be exact: a rotated square's corner region is outside."""
    rows = [_row("dr1", "2025W20_1B_0001_1", VER, 60800.0, 100.0, 20.0, rot=30.0)]
    _write(idx_path, rows)
    assert len(_find(100.0, 20.0)) == 1

    def off(dist, ang):  # tangent-plane offset, ang measured from east toward north
        a = np.deg2rad(ang)
        return 100.0 + dist * np.cos(a) / np.cos(np.deg2rad(20)), 20.0 + dist * np.sin(a)

    # rotated-corner direction (75 deg) is inside at 2.3 deg; 45 deg (an unrotated corner) is not
    assert len(_find(*off(2.3, 75.0))) == 1
    assert len(_find(*off(2.3, 45.0))) == 0
    assert len(_find(100.0, 20.0 + 1.2)) == 1
    assert len(_find(100.0, 20.0 + 3.0)) == 0
    assert len(_find(280.0, -20.0)) == 0  # antipode: no spurious hit


def test_ra_wraparound(idx_path):
    """Footprints straddling RA=0 must work; naive RA min/max boxes break here."""
    _write(idx_path, [_row("dr1", "2025W20_1B_0002_1", VER, 60800.0, 0.2, 10.0)])
    assert len(_find(359.5, 10.0)) == 1
    assert len(_find(1.0, 10.0)) == 1


def test_sorted_and_d3_key_derivation(idx_path):
    """D3 shares pointing with D6, so the D3 key is derived; order is by time for blinking."""
    rows = [
        _row("dr1", "2025W20_1B_0002_1", VER, 60802.0, 100.0, 20.0),
        _row("dr1", "2025W20_1B_0001_1", VER, 60801.0, 100.0, 20.0),
    ]
    _write(idx_path, rows)
    refs = _find(100.0, 20.0, det=3)
    assert [r.t_mjd for r in refs] == [60801.0, 60802.0]
    r = refs[0]
    assert r.detector == 3 and r.obs_id == "2025W20_1B_0001_1_D3"
    assert r.uri == _key("dr1", "2025W20_1B", "2025W20_1B_0001_1", VER, det=3)
    assert index.parse_key(r.uri)["det"] == "3"


def test_unindexed_detector_raises(idx_path):
    """Silent empty results for an unknown detector would look like 'no coverage'."""
    _write(idx_path, [])
    with pytest.raises(ValueError, match="not indexed"):
        _find(100.0, 20.0, det=7)


def test_d1_d2_derive_from_their_pointing_pair():
    """D1/D4 and D2/D5 share pointing (header CRVAL checked 2026-11-14), so a D4 index row
    serves D1 and a D5 row serves D2. A wrong pair would fetch frames of a different sky patch."""
    k4 = _key("dr1", "2025W20_1B", "2025W20_1B_0001_1", VER, det=4)
    k5 = _key("dr1", "2025W20_1B", "2025W20_1B_0001_1", VER, det=5)
    assert index.derive_key(k4, 1) == _key("dr1", "2025W20_1B", "2025W20_1B_0001_1", VER, det=1)
    assert index.derive_key(k5, 2) == _key("dr1", "2025W20_1B", "2025W20_1B_0001_1", VER, det=2)
    assert index.derive_key(k4, 4) == k4


async def test_missing_d4_index_fails_loud(tmp_path, monkeypatch):
    """Before the D4 index is built, D1 must raise, not return [] ('no coverage')."""
    monkeypatch.setitem(config.S3_INDEX_PATHS, 4, tmp_path / "missing_D4.csv.gz")
    index._cache.clear()
    with pytest.raises(FileNotFoundError):
        await index.find_images(100.0, 20.0, det=1)


def test_dedupe_release_priority_and_version(idx_path):
    """C2: DR1 beats QR3; within a release the numerically larger version wins (not lexicographic)."""
    exp = "2025W20_1B_0001_1"
    rows = [
        _row("qr3", exp, "l2b-v27-2026-196", 60800.0, 100.0, 20.0),
        _row("dr1", exp, "l2b-v9-2026-196", 60800.0, 100.0, 20.0),
        _row("dr1", exp, "l2b-v27-2026-196", 60800.0, 100.0, 20.0),
        _row("qr3", "2025W20_1B_0003_1", "l2b-v27-2026-196", 60805.0, 100.0, 20.0),
        _row("qr3", "2025W20_1B_0003_1", "l2b_retry-v27-2026-197", 60805.0, 100.0, 20.0),
    ]
    _write(idx_path, rows)
    refs = _find(100.0, 20.0)
    got = {r.exposure_id: (r.release, r.version) for r in refs}
    assert got[exp] == ("dr1", "l2b-v27-2026-196")
    assert got["2025W20_1B_0003_1"] == ("qr3", "l2b_retry-v27-2026-197")
    assert len(refs) == 2


def test_truncated_index_tail_is_tolerated(idx_path):
    """A build killed mid-write leaves a truncated gzip; earlier rows must still load."""
    rows = [_row("dr1", f"2025W20_1B_{i:04d}_1", VER, 60800.0 + i, 100.0, 20.0) for i in range(50)]
    _write(idx_path, rows, truncate=True)
    assert len(_find(100.0, 20.0)) >= 1


def test_missing_index_fails_loud(idx_path):
    with pytest.raises(FileNotFoundError):
        _find(100.0, 20.0)


def test_data_version():
    assert index.data_version() == "sprint-2026-11-15-b"
