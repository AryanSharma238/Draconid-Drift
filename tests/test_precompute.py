"""scripts/precompute.py with stubbed index/fetch: resumable, idempotent, table output. No network."""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from backend import cache, config, fetch, index, motion
from backend.models import make_grid
from tests.test_motion_api import MOVER_T, SceneStubs, mover_scene
from tests.test_sequence import Stubs, make_refs

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))  # precompute.py does `from targets import ...` like the other scripts
_spec = importlib.util.spec_from_file_location("precompute", SCRIPTS / "precompute.py")
precompute = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(precompute)


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = Stubs(make_refs())
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    return s


def args(**kw) -> argparse.Namespace:
    base = {
        "targets": "wise0855,rdra",
        "dets": "3,6",
        "modes": "pass,all",
        "size": 0.1,
        "force": False,
        "extras": "",
    }
    return argparse.Namespace(**{**base, **kw})


async def test_fills_l2_then_second_run_skips_everything(stubs, capsys):
    # why: the overnight run must be restartable without redoing hours of S3 reads
    assert await precompute.main(args()) == 0
    first_calls = stubs.fetch_calls
    assert first_calls == 2 * 2 * 2  # targets x dets x modes
    out = capsys.readouterr().out
    assert "target" in out and "seconds" in out and out.count("cached") >= 8

    assert await precompute.main(args()) == 0
    assert stubs.fetch_calls == first_calls  # all skipped (L2 hit)
    assert capsys.readouterr().out.count("skip (L2 hit)") >= 8


async def test_force_recomputes(stubs):
    await precompute.main(args(targets="rdra", dets="6", modes="all"))
    await precompute.main(args(targets="rdra", dets="6", modes="all", force=True))
    assert stubs.fetch_calls == 2


async def test_uncached_result_gives_exit_code_1(stubs):
    stubs.fail = {0, 1, 2}  # >= 20 % failed -> sequence_stream refuses to cache
    assert await precompute.main(args(targets="rdra", dets="6", modes="all")) == 1


async def test_no_coverage_is_reported_not_an_error(stubs, capsys):
    stubs.refs = []
    assert await precompute.main(args(targets="rdra", dets="6", modes="all")) == 0
    assert "no coverage" in capsys.readouterr().out


def test_unknown_target_exits():
    with pytest.raises(SystemExit):
        precompute._csv_arg("nope", ("a", "b"), str, "target")


@pytest.fixture
def mover_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = SceneStubs(make_refs(MOVER_T), mover_scene)
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    monkeypatch.setattr(
        motion, "lambda_matched_lightcurve", lambda frames, x, y: [{"dmag": 0.0}], raising=False
    )
    monkeypatch.setattr(
        motion,
        "lightcurve_summary",
        lambda pts: {"per_pass_dmag": [], "chi2_red_constant": 1.0},
        raising=False,
    )
    return s


async def test_extras_fill_motion_lmdiff_and_lightcurve_only_for_variables(mover_stubs, capsys):
    # why: a fixed-aperture light curve on a high-PM target is meaningless, so wise0855 must be skipped
    # (and the table must say so), while the variable rdra gets one
    a = args(targets="wise0855,rdra", dets="6", modes="pass", extras="motion,lightcurve,lmdiff")
    assert await precompute.main(a) == 0
    out = capsys.readouterr().out
    grid = lambda t: make_grid(*precompute.TARGETS[t], 0.1)
    dv = index.data_version()
    for t in ("wise0855", "rdra"):
        assert cache.json_get(cache.json_key("motion", grid(t), 6, dv)) is not None
        assert cache.l2_get(cache.stack_key(grid(t), 6, "lmdiff", dv)) is not None
    assert cache.json_get(cache.json_key("lightcurve", grid("rdra"), 6, dv)) is not None
    assert cache.json_get(cache.json_key("lightcurve", grid("wise0855"), 6, dv)) is None
    assert "skipped (not a variable" in out

    calls = mover_stubs.fetch_calls
    assert await precompute.main(a) == 0  # resumable: everything is an L2 hit now
    assert mover_stubs.fetch_calls == calls
    assert "skip (L2 hit)" in capsys.readouterr().out


async def test_extras_default_is_all_four_and_empty_means_none():
    assert precompute.EXTRAS == ("motion", "lightcurve", "lmdiff", "spectrum")
    assert precompute._csv_arg("", precompute.EXTRAS, str, "extra") == []


async def test_unavailable_lightcurve_makes_exit_code_1(mover_stubs, monkeypatch, capsys):
    # why: if Track D's functions are not merged, an overnight run must end loudly, not report success
    monkeypatch.delattr(motion, "lambda_matched_lightcurve", raising=False)
    assert await precompute.main(args(targets="rdra", dets="6", modes="pass", extras="lightcurve")) == 1
    assert "lightcurve not available" in capsys.readouterr().out


async def test_spectrum_extra_uses_catalogue_pm_only_for_the_five_test_targets(monkeypatch, tmp_path, capsys):
    # why: the brown dwarf moves ~8"/yr; a PM-less aperture would miss it. The PM must reach spectrum_result,
    # and the second run must be a resumable skip.
    from tests.test_spectrum import SpecStubs, flat_scene, install

    install(monkeypatch, tmp_path, SpecStubs([60000.0, 60100.0, 60200.0], flat_scene))
    seen: list[tuple] = []
    real = precompute.spectrum_result

    async def spy(ra, dec, size, pmra, pmdec, refresh=False):
        seen.append((pmra, pmdec))
        return await real(ra, dec, size, pmra, pmdec, refresh=refresh)

    monkeypatch.setattr(precompute, "spectrum_result", spy)
    a = args(targets="wise0855,luhman16", dets="6", modes="pass", extras="spectrum")
    assert await precompute.main(a) == 0
    out = capsys.readouterr().out
    assert seen == [(precompute.CATALOG["wise0855"][2], precompute.CATALOG["wise0855"][3])]
    assert "skipped (not one of the 5 test targets)" in out
    assert await precompute.main(a) == 0
    assert len(seen) == 1 and "skip (L2 hit)" in capsys.readouterr().out
