import pytest

from backend import ids
from backend.models import ImageRef

# Real filenames supplied by Track A (verified against the S3 mirror, 2026-11-14).
DR1_NAME = "level2_2025W17_4B_0001_1D6_spx_l2b-v27-2026-196.fits"
RETRY_NAME = "level2_2025W17_4B_0200_3D3_spx_l2b_retry-v27-2026-197.fits"
QR2_NAME = "level2_2025W17_4B_0001_1D3_spx_l2b-v20-2025-240.fits"
C2_EXAMPLE = "level2_2025W19_2B_0346_1D2_spx_l2b-v20-2025-247.fits"


def ref(obs_id="E_D1", release="DR1", version="l2b-v20-2025-247", t=60000.0) -> ImageRef:
    return ImageRef(obs_id, obs_id.split("_D")[0], 1, t, release, version, f"x/{obs_id}.fits")


def test_parse_c2_example():
    r = ids.parse_uri(f"dr1/level2/2025W19/{C2_EXAMPLE}", 60812.31, "DR1")
    assert (r.exposure_id, r.detector, r.version) == ("2025W19_2B_0346_1", 2, "l2b-v20-2025-247")
    assert r.obs_id == "2025W19_2B_0346_1_D2"
    assert (r.release, r.t_mjd) == ("DR1", 60812.31)


def test_parse_real_names_including_retry():
    # Why: one unparseable naming variant would silently drop frames. `l2b_retry` is real.
    a = ids.parse_uri(DR1_NAME, 1.0, "DR1")
    assert (a.obs_id, a.version) == ("2025W17_4B_0001_1_D6", "l2b-v27-2026-196")
    b = ids.parse_uri(RETRY_NAME, 1.0, "DR1")
    assert (b.obs_id, b.detector, b.version) == ("2025W17_4B_0200_3_D3", 3, "l2b_retry-v27-2026-197")
    assert ids.parse_uri(QR2_NAME, 1.0, "QR2").version == "l2b-v20-2025-240"


def test_parse_drops_query_string():
    assert "?" not in ids.parse_uri(f"a/b/{DR1_NAME}?x=1", 1.0, "DR1").uri


@pytest.mark.parametrize(
    "bad",
    [
        "level2_2025W17_4B_0001_1D7_spx_l2b-v27-2026-196.fits",  # detector 7
        "level2_2025W17_4B_0001_1D6_spx_l2b-v27-2026-196.fits.gz",
        "level2_2025W17_4B_0001_1D6_spx_l2c-v27-2026-196.fits",
        "garbage.fits",
    ],
)
def test_parse_rejects_non_matching_and_names_the_uri(bad):
    with pytest.raises(ValueError, match="unparseable") as e:
        ids.parse_uri(bad, 1.0, "DR1")
    assert bad in str(e.value)


def test_dedupe_dr1_beats_qr_even_with_older_version():
    out = ids.dedupe([ref(release="QR3", version="l2b-v30-2026-300"), ref(release="DR1")])
    assert [r.release for r in out] == ["DR1"]


def test_dedupe_release_order_dr1_qr3_qr2():
    out = ids.dedupe([ref(release="QR2", version="l2b-v99-2026-300"), ref(release="QR3")])
    assert out[0].release == "QR3"


def test_dedupe_version_is_numeric_not_lexicographic():
    # Why: 'v9' > 'v10' as strings; and '_' sorts after '-', so a lower retry would beat a higher main.
    out = ids.dedupe(
        [ref(release="QR3", version="l2b-v9-2026-100"), ref(release="QR3", version="l2b-v10-2026-050")]
    )
    assert out[0].version == "l2b-v10-2026-050"
    out = ids.dedupe([ref(version="l2b_retry-v20-2025-247"), ref(version="l2b-v27-2026-196")])
    assert out[0].version == "l2b-v27-2026-196"


def test_dedupe_retry_wins_only_a_tie():
    out = ids.dedupe([ref(version="l2b-v27-2026-196"), ref(version="l2b_retry-v27-2026-196")])
    assert out[0].version == "l2b_retry-v27-2026-196"


def test_dedupe_sorted_by_time_and_keeps_distinct_obs_ids():
    out = ids.dedupe(
        [ref("B_D1", t=3.0), ref("A_D1", t=1.0), ref("A_D1", release="QR3", t=1.0), ref("C_D1", t=2.0)]
    )
    assert [r.obs_id for r in out] == ["A_D1", "C_D1", "B_D1"]


def test_unknown_release_fails_loud():
    with pytest.raises(ValueError, match="release"):
        ids.dedupe([ref(release="DR1"), ref(release="beta")])
