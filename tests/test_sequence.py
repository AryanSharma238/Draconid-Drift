"""sequence_stream: stubbed index + fetch (no network). Shared stubs live here; test_api.py imports them."""

import base64
import gzip

import numpy as np
import orjson
import pytest

from backend import cache, config, fetch, index, process
from backend.models import Frame, ImageRef, make_grid
from backend.sequence import _pass_frame, group_refs_by_pass, interleave_order, sequence_stream
from backend.timing import Timer

RA, DEC, SIZE, DET = 133.76, -7.24, 0.1, 6
# 3 survey passes (gaps > PASS_GAP_DAYS between them): 3 + 4 + 3 exposures
MJDS = [60000.0, 60000.5, 60001.0, 60100.0, 60100.5, 60101.0, 60101.5, 60200.0, 60200.5, 60201.0]
PASS_SIZES = [3, 4, 3]


def make_refs(mjds=MJDS) -> list[ImageRef]:
    return [
        ImageRef(f"e{i}_D{DET}", f"e{i}", DET, m, "dr1", "l2b-v1-2026-001", f"k/{i}.fits")
        for i, m in enumerate(mjds)
    ]


def make_frame(ref: ImageRef, grid, value: float | None = None) -> Frame:
    """Constant-valued frame (value defaults to the exposure index) with a unit variance map."""
    v = float(ref.exposure_id[1:]) if value is None else value
    shape = (grid.npix, grid.npix)
    return Frame(
        ref,
        np.full(shape, v, np.float32)
        + np.random.default_rng(int(v)).normal(0, 0.1, shape).astype(np.float32),
        np.ones(shape, np.float32),
        np.zeros(shape, np.uint32),
        4.5,
    )


class Stubs:
    """Replaces index.find_images and fetch.get_frames; records calls. Frames complete in REVERSE order of the
    refs get_frames receives (or in submission order with in_order=True)."""

    def __init__(self, refs: list[ImageRef], fail: set[int] = frozenset(), in_order: bool = False) -> None:
        self.refs, self.fail, self.in_order = refs, set(fail), in_order
        self.find_calls = 0
        self.fetch_calls = 0
        self.submitted: list[list[str]] = []  # obs_ids in the order get_frames was handed them

    async def find_images(self, ra, dec, det):
        self.find_calls += 1
        return list(self.refs)

    async def get_frames(self, refs, grid):
        self.fetch_calls += 1
        self.submitted.append([r.obs_id for r in refs])
        for i in range(len(refs)) if self.in_order else reversed(range(len(refs))):
            if i in self.fail:
                yield i, None, "TimeoutError: stub"
            else:
                yield i, make_frame(refs[i], grid), None


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = Stubs(make_refs())
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    return s


async def drain(mode="all", det=DET, size=SIZE) -> list[dict]:
    return [orjson.loads(ln) async for ln in sequence_stream(RA, DEC, size, det, mode, Timer())]


def f16(b64: str, npix: int) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), "<f2").reshape(npix, npix).astype(np.float32)


def read_l2(mode="all") -> list[dict]:
    key = cache.stack_key(make_grid(RA, DEC, SIZE), DET, mode, index.data_version())
    path = cache.l2_get(key)
    assert path is not None, "expected an L2 file"
    return [orjson.loads(ln) for ln in gzip.decompress(path.read_bytes()).splitlines()]


def test_group_refs_by_pass_agrees_with_group_by_pass():
    # why: pass membership must be known BEFORE fetching, and must match what process does afterwards
    refs = make_refs()
    grid = make_grid(RA, DEC, SIZE)
    frames = process.group_by_pass([make_frame(r, grid) for r in refs])
    assert group_refs_by_pass(refs) == [f.pass_idx for f in frames]
    assert [group_refs_by_pass(refs).count(p) for p in range(3)] == PASS_SIZES


async def test_all_mode_message_order_and_content(stubs):
    msgs = await drain("all")
    assert [m["type"] for m in msgs[:2]] == ["status", "header"]
    assert msgs[-1]["type"] == "done"
    hdr, done = msgs[1], msgs[-1]
    assert hdr["n_expected"] == len(MJDS) and hdr["zscale"] is None and hdr["source"] == "live"
    frames = [m for m in msgs if m["type"] == "frame"]
    assert len(frames) == done["n_frames"] == len(MJDS) and done["n_failed"] == 0
    # `i` is the index in t_mjd order even though frames are fetched interleaved and arrive out of order
    assert sorted(m["i"] for m in frames) == list(range(len(MJDS)))
    assert [m["i"] for m in frames] != sorted(m["i"] for m in frames)
    assert {m["i"]: m["t_mjd"] for m in frames} == dict(enumerate(MJDS))
    assert [m["pass"] for m in sorted(frames, key=lambda m: m["i"])] == [0, 0, 0, 1, 1, 1, 1, 2, 2, 2]
    assert all("flags" in m and m["kind"] == "single" for m in frames)
    lo, hi = done["zscale"]
    assert lo < hi


async def test_zscale_is_stack_wide_over_all_frames(stubs):
    # why: C9 says the zscale is computed over the WHOLE stack, not per frame (frames here differ by
    # their constant level, so a per-frame limit would be tiny compared to the stack-wide one)
    msgs = await drain("all")
    grid = make_grid(RA, DEC, SIZE)
    arrival = [stubs.refs[k] for k in reversed(interleave_order(group_refs_by_pass(stubs.refs)))]
    expect = process.zscale_limits([make_frame(r, grid).data for r in arrival])  # ZScale subsamples by stride
    assert tuple(msgs[-1]["zscale"]) == pytest.approx(expect)
    assert msgs[-1]["zscale"][1] - msgs[-1]["zscale"][0] > 2.0


async def test_pass_mode_one_coadd_per_pass(stubs):
    msgs = await drain("pass")
    assert msgs[1]["n_expected"] == 3
    frames = sorted((m for m in msgs if m["type"] == "frame"), key=lambda m: m["i"])
    assert [m["i"] for m in frames] == [0, 1, 2]
    assert [m["n_coadd"] for m in frames] == PASS_SIZES
    assert [m["obs_id"] for m in frames] == [f"pass{p}_D{DET}" for p in range(3)]
    assert all(m["kind"] == "pass" and "flags" not in m for m in frames)
    # inverse-variance (here: equal-weight) mean of exposures 0,1,2 / 3..6 / 7..9
    npix = make_grid(RA, DEC, SIZE).npix
    for m, expect in zip(frames, [1.0, 4.5, 8.0]):
        assert np.nanmedian(f16(m["data"], npix)) == pytest.approx(expect, abs=0.05)
    assert msgs[-1]["n_frames"] == 3


async def test_failed_frame_inside_pass_keeps_pass_membership(monkeypatch, tmp_path):
    # why: exposures 25 d apart are one pass; if the middle one fails, the survivors are 50 d apart and
    # process.coadd_by_pass would split them into two passes. Pass membership must come from the refs.
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = Stubs(make_refs([60000.0, 60025.0, 60050.0]), fail={1})
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    msgs = await drain("pass")
    frames = [m for m in msgs if m["type"] == "frame"]
    assert len(frames) == 1 and frames[0]["n_coadd"] == 2 and frames[0]["pass"] == 0
    assert [m["type"] for m in msgs].count("error") == 1
    assert msgs[-1]["n_failed"] == 1


def test_pass_frame_matches_coadd_by_pass():
    grid = make_grid(RA, DEC, SIZE)
    refs = make_refs()[:3]
    mine = _pass_frame(0, [make_frame(r, grid) for r in refs])
    ref = process.coadd_by_pass([make_frame(r, grid) for r in refs])[0]
    np.testing.assert_array_equal(mine.data, ref.data)
    assert (mine.ref.obs_id, mine.ref.t_mjd, mine.lambda_um) == (ref.ref.obs_id, ref.ref.t_mjd, ref.lambda_um)


async def test_l2_written_with_final_zscale_and_sorted_frames(stubs):
    live = await drain("all")
    stored = read_l2("all")
    assert stored[0]["type"] == "header" and stored[0]["source"] == "cache"
    assert stored[0]["zscale"] == live[-1]["zscale"]  # C9: stored header carries the final zscale
    assert [m["i"] for m in stored if m["type"] == "frame"] == list(range(len(MJDS)))  # sorted by i
    assert not any(m["type"] in ("status", "zscale") for m in stored)
    assert stored[-1]["type"] == "done"


async def test_few_failures_still_cached(stubs):
    stubs.fail = {3}  # 1/10 < 20 %
    msgs = await drain("all")
    assert msgs[-1]["n_failed"] == 1
    assert (
        cache.l2_get(cache.stack_key(make_grid(RA, DEC, SIZE), DET, "all", index.data_version())) is not None
    )


async def test_failure_rate_over_threshold_not_cached(stubs):
    # why: a cached broken result would be served (immutably) forever
    stubs.fail = {0, 1, 2}  # 3/10 >= 20 %
    msgs = await drain("all")
    assert msgs[-1]["n_failed"] == 3 and msgs[-1]["n_frames"] == 7
    assert cache.l2_get(cache.stack_key(make_grid(RA, DEC, SIZE), DET, "all", index.data_version())) is None
    assert not list((config.STACK_CACHE_DIR).rglob("*.tmp"))


async def test_no_coverage_header_then_done_and_not_cached(stubs):
    stubs.refs = []
    msgs = await drain("all")
    assert [m["type"] for m in msgs] == ["status", "header", "done"]
    assert msgs[1]["n_expected"] == 0 and stubs.fetch_calls == 0
    assert not config.STACK_CACHE_DIR.exists() or not list(config.STACK_CACHE_DIR.rglob("*.gz"))


async def test_missing_index_is_an_error_line_not_a_crash(monkeypatch, tmp_path):
    async def boom(ra, dec, det):
        raise FileNotFoundError("s3_footprints.csv.gz")

    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    monkeypatch.setattr(index, "find_images", boom)
    msgs = await drain("all")
    assert [m["type"] for m in msgs] == ["status", "header", "error", "done"]
    assert "s3_footprints" in msgs[2]["reason"] and msgs[-1]["n_failed"] == 1


async def test_bad_mode_raises(stubs):
    with pytest.raises(ValueError):
        await drain("bogus")


# ------------------------------------------------------------------ interleaved fetch + pass previews (C5)
def test_interleave_order_is_round_robin_across_passes():
    # passes of 3 + 4 + 3: p0f0 p1f0 p2f0 p0f1 p1f1 p2f1 p0f2 p1f2 p2f2 p1f3
    assert interleave_order(group_refs_by_pass(make_refs())) == [0, 3, 7, 1, 4, 8, 2, 5, 9, 6]
    assert interleave_order([]) == []


async def test_fetch_is_submitted_interleaved_but_i_stays_time_sorted(stubs):
    # why: C5 says `i` is the t_mjd-sorted index; only the SUBMISSION order may change (round-robin)
    msgs = await drain("all")
    assert stubs.submitted == [[f"e{k}_D{DET}" for k in (0, 3, 7, 1, 4, 8, 2, 5, 9, 6)]]
    by_i = {m["i"]: m for m in msgs if m["type"] == "frame"}
    assert {i: m["obs_id"] for i, m in by_i.items()} == {i: f"e{i}_D{DET}" for i in range(len(MJDS))}
    assert {i: m["t_mjd"] for i, m in by_i.items()} == dict(enumerate(MJDS))


def three_pass_stack(n_per_pass: int, fail: set[int] = frozenset()) -> Stubs:
    mjds = [60000.0 + 100 * p + 0.1 * k for p in range(3) for k in range(n_per_pass)]
    return Stubs(make_refs(mjds), fail, in_order=True)  # completes in SUBMISSION order


@pytest.fixture
def stubs20(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = three_pass_stack(20)
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    return s


def pass_frames(msgs: list[dict]) -> list[tuple[int, dict]]:
    """(line number, frame msg) in stream order."""
    return [(n, m) for n, m in enumerate(msgs) if m["type"] == "frame"]


async def test_every_pass_has_a_preview_before_any_pass_finishes(stubs20):
    # why: the user-visible win. A 3-pass blink must be possible after ~PASS_PREVIEW_FRAMES*3 fetches, not
    # after the first pass's 20 frames: the first line of EVERY pass must precede ANY pass's final line.
    msgs = await drain("pass")
    fr = pass_frames(msgs)
    finals = [n for n, m in fr if not m.get("preview")]
    first_of_pass = [min(n for n, m in fr if m["i"] == p) for p in range(3)]
    assert len(finals) == 3 and max(first_of_pass) < min(finals)
    assert all(next(m for n, m in fr if n == first_of_pass[p]).get("preview") is True for p in range(3))


async def test_final_replaces_preview_same_i_no_preview_key_full_n_coadd(stubs20):
    # why: the client REPLACES frame i; the final must carry every successful input of its pass
    msgs = await drain("pass")
    for p in range(3):
        lines = [m for _, m in pass_frames(msgs) if m["i"] == p]
        assert len(lines) == 2
        prev, final = lines
        assert prev["preview"] is True and prev["n_coadd"] == config.PASS_PREVIEW_FRAMES
        assert "preview" not in final and final["n_coadd"] == 20 and final["pass"] == p
        assert (
            prev["obs_id"] == final["obs_id"] == f"pass{p}_D{DET}" and prev["kind"] == final["kind"] == "pass"
        )
    done = msgs[-1]
    assert done["n_frames"] == 3  # previews are not frames of the stack
    assert next(m for m in msgs if m["type"] == "header")["n_expected"] == 3


async def test_l2_holds_only_final_pass_frames(stubs20):
    # why: stored stacks are served as-is; a preview there would be shown as a (noisy) final frame
    await drain("pass")
    stored = read_l2("pass")
    frames = [m for m in stored if m["type"] == "frame"]
    assert [m["i"] for m in frames] == [0, 1, 2]
    assert not any("preview" in m for m in stored)
    assert [m["n_coadd"] for m in frames] == [20, 20, 20]


async def test_pass_with_few_inputs_gets_only_the_final_frame(stubs):
    # passes of 3, 4, 3 inputs with PASS_PREVIEW_FRAMES = 4: nothing to preview (<= 4 inputs)
    assert max(PASS_SIZES) <= config.PASS_PREVIEW_FRAMES
    msgs = await drain("pass")
    frames = [m for m in msgs if m["type"] == "frame"]
    assert len(frames) == 3 and not any("preview" in m for m in frames)


async def test_preview_counts_successful_inputs_only(monkeypatch, tmp_path):
    # why: a failed fetch must not count towards the preview threshold (n_coadd would be < PASS_PREVIEW_FRAMES)
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = three_pass_stack(20, fail={0, 3})  # submission slots 0 and 3 = the first two exposures of pass 0
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    msgs = await drain("pass")
    previews = [m for m in msgs if m["type"] == "frame" and m.get("preview")]
    assert [m["n_coadd"] for m in previews] == [config.PASS_PREVIEW_FRAMES] * 3
    final0 = next(m for m in msgs if m["type"] == "frame" and m["i"] == 0 and not m.get("preview"))
    assert final0["n_coadd"] == 18


async def test_all_mode_never_sends_previews(stubs20):
    msgs = await drain("all")
    assert not any("preview" in m for m in msgs) and msgs[-1]["n_frames"] == 60
