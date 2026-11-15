"""HTTP layer over stubbed index/fetch (stubs from test_sequence). No network."""

import gzip
import types

import httpx
import orjson
import pytest

from backend import app as app_mod
from backend import cache, config, fetch, index, resolve
from backend.app import app
from backend.models import make_grid
from tests.test_sequence import DEC, DET, MJDS, RA, SIZE, Stubs, make_refs

Q = {"ra": RA, "dec": DEC, "size": SIZE, "det": DET, "mode": "all"}


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    s = Stubs(make_refs())
    monkeypatch.setattr(index, "find_images", s.find_images)
    monkeypatch.setattr(fetch, "get_frames", s.get_frames)
    return s


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def lines(r: httpx.Response) -> list[dict]:
    return [orjson.loads(ln) for ln in r.text.splitlines()]


@pytest.mark.parametrize(
    "bad",
    [
        {"size": 0.33},
        {"det": 7},
        {"dec": 95},
        {"mode": "bogus"},
        {"ra": "nan"},
        {"ra": "abc"},
    ],
)
async def test_invalid_params_422_with_json_error(client, stubs, bad):
    r = await client.get("/api/sequence", params={**Q, **bad})
    assert r.status_code == 422
    assert isinstance(r.json()["error"], str)
    assert stubs.find_calls == 0  # rejected before any work


async def test_missing_required_param_422(client, stubs):
    r = await client.get("/api/sequence", params={"ra": 10})
    assert r.status_code == 422 and "dec" in r.json()["error"]


async def test_stream_starts_with_header_ends_with_done(client, stubs):
    stubs.fail = {2}
    r = await client.get("/api/sequence", params=Q)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["cache-control"] == "no-store"
    msgs = lines(r)
    types_ = [m["type"] for m in msgs]
    assert next(t for t in types_ if t != "status") == "header" and types_[-1] == "done"
    n_expected = next(m for m in msgs if m["type"] == "header")["n_expected"]
    assert types_.count("frame") == n_expected - types_.count("error") == len(MJDS) - 1


async def test_second_identical_request_served_from_l2_without_fetching(client, stubs):
    # why: the cache is the main speed mechanism; a hit must not touch the index or S3
    first = await client.get("/api/sequence", params=Q)
    assert stubs.fetch_calls == 1
    second = await client.get("/api/sequence", params=Q)
    assert stubs.fetch_calls == 1 and stubs.find_calls == 1
    assert second.headers["content-encoding"] == "gzip"
    # why: until the 5-target check passes, a stack fix must reach browsers within minutes, not a year
    cc = second.headers["cache-control"]
    assert "immutable" not in cc and f"max-age={config.L2_MAX_AGE_S}" in cc
    assert config.L2_MAX_AGE_S <= 3600
    m2 = lines(second)
    assert m2[0]["type"] == "header" and m2[0]["source"] == "cache"
    live_frames = sorted((m for m in lines(first) if m["type"] == "frame"), key=lambda m: m["i"])
    assert [m for m in m2 if m["type"] == "frame"] == live_frames


async def test_l2_hit_uses_snapped_grid_key(client, stubs):
    # why: requests 1 arcsec apart share a cache entry (C3 snap)
    await client.get("/api/sequence", params=Q)
    await client.get("/api/sequence", params={**Q, "ra": RA + 0.0001})
    assert stubs.fetch_calls == 1


async def test_failure_rate_20pct_or_more_not_cached(client, stubs):
    stubs.fail = {0, 1}  # 2/10
    await client.get("/api/sequence", params=Q)
    await client.get("/api/sequence", params=Q)
    assert stubs.fetch_calls == 2  # nothing was cached, so the second request recomputed
    assert cache.l2_get(cache.stack_key(make_grid(RA, DEC, SIZE), DET, "all", index.data_version())) is None


async def test_pass_mode_frame_per_pass(client, stubs):
    r = await client.get("/api/sequence", params={**Q, "mode": "pass"})
    frames = [m for m in lines(r) if m["type"] == "frame"]
    assert len(frames) == 3 and all(m["n_coadd"] >= 1 for m in frames)


async def test_ra_is_normalised(client, stubs):
    r = await client.get("/api/sequence", params={**Q, "ra": RA - 360})
    assert r.status_code == 200
    hdr = next(m for m in lines(r) if m["type"] == "header")
    assert hdr["ra"] == pytest.approx(RA, abs=1e-6)


async def test_cors_header(client, stubs):
    r = await client.get("/api/sequence", params=Q, headers={"Origin": "http://elsewhere"})
    assert r.headers["access-control-allow-origin"] == "*"
    r = await client.get("/api/sequence", params={**Q, "det": 7}, headers={"Origin": "http://elsewhere"})
    assert r.status_code == 422 and r.headers["access-control-allow-origin"] == "*"


async def test_resolve_route(client, monkeypatch):
    payload = {"name": "X", "ra": 1.0, "dec": 2.0, "epoch": 2000.0, "pmra": 0.0, "pmdec": 0.0,
               "ra_now": 1.0, "dec_now": 2.0, "epoch_now": 2025.9}  # fmt: skip
    monkeypatch.setattr(resolve, "resolve", lambda q: payload if q == "X" else None)
    r = await client.get("/api/resolve", params={"q": "X"})
    assert r.status_code == 200 and r.json() == payload and "max-age" in r.headers["cache-control"]
    r = await client.get("/api/resolve", params={"q": "nope"})
    assert r.status_code == 404 and r.json() == {"error": "not found"}


async def test_health_ok_and_missing_index(client, monkeypatch):
    monkeypatch.setattr(index, "_load", lambda p: types.SimpleNamespace(rows=[1, 2, 3]))
    r = await client.get("/api/health")
    assert r.json() == {"ok": True, "data_version": index.data_version(), "index_rows": 3}

    def missing(p):
        raise FileNotFoundError(p)

    monkeypatch.setattr(index, "_load", missing)
    r = await client.get("/api/health")
    assert r.status_code == 503 and r.json()["ok"] is False


async def test_static_frontend_mounted_and_api_wins(client, stubs):
    r = await client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert (await client.get("/api/health")).headers["content-type"].startswith("application/json")


def test_l2_file_is_valid_gzip_ndjson(stubs):
    w = cache.l2_put_stream("v/x_1")
    w.write(b'{"type":"header"}\n')
    p = w.commit()
    assert gzip.decompress(p.read_bytes()) == b'{"type":"header"}\n'
    assert p.stat().st_mode & 0o777 == 0o644  # readable by whatever user serves the rsynced cache
    w2 = cache.l2_put_stream("v/y_1")
    w2.write(b"x")
    w2.abort()
    assert not [f for f in p.parent.iterdir() if f.suffix == ".tmp"]


# ------------------------------------------------------------------ startup warm-up (no real processes)
class FakePool:
    """Stands in for a ProcessPoolExecutor: records submissions, runs them inline."""

    def __init__(self, fail: bool = False) -> None:
        self.jobs: list[tuple] = []
        self.fail = fail

    def submit(self, fn, *args, **kw):
        import concurrent.futures

        if self.fail:
            raise RuntimeError("pool is broken")
        self.jobs.append((fn, args, kw))
        f: concurrent.futures.Future = concurrent.futures.Future()
        f.set_result(len(self.jobs))
        return f


@pytest.fixture
def fake_pools(monkeypatch):
    proc, read = FakePool(), FakePool()
    loaded: list = []
    monkeypatch.setattr(fetch, "_get_pool", lambda: proc)
    monkeypatch.setattr(fetch, "_get_read_pool", lambda: read)
    monkeypatch.setattr(index, "_load", lambda path: loaded.append(path))
    return proc, read, loaded


async def test_lifespan_warms_both_pools_and_the_index(fake_pools):
    # why: the first real request must not pay process spawn + astropy import (~3-4 s measured on EC2).
    # One job per worker: a busy worker forces the executor to spawn the next one, so all N exist afterwards.
    proc, read, loaded = fake_pools
    async with app.router.lifespan_context(app):
        pass
    assert len(proc.jobs) == config.PROCESS_WORKERS and len(read.jobs) == config.S3_READ_WORKERS
    assert all(fn is app_mod.warm_worker for fn, _, _ in proc.jobs + read.jobs)
    assert all(kw == {"s3": False} for _, _, kw in proc.jobs) and all(
        kw == {"s3": True} for _, _, kw in read.jobs
    )
    assert config.S3_INDEX_PATH in loaded  # D6 index loaded (the others too, where the file exists)


async def test_warmup_failure_is_logged_loudly_but_startup_continues(fake_pools, caplog):
    proc, _, _ = fake_pools
    proc.fail = True
    async with app.router.lifespan_context(app):  # must not raise
        pass
    assert any(r.levelname == "ERROR" and "warm-up FAILED" in r.getMessage() for r in caplog.records)


async def test_warmup_missing_index_is_a_warning_not_a_failure(monkeypatch, fake_pools, caplog):
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(index, "_load", missing)
    dt = await app_mod.warm_up()
    assert dt >= 0
    assert not any("warm-up FAILED" in r.getMessage() for r in caplog.records)
    assert any("not present" in r.getMessage() for r in caplog.records)


def test_warm_worker_imports_what_the_jobs_need_and_returns_pid():
    import os

    from backend.sequence import warm_worker

    assert warm_worker(modules=("backend.process",), s3=False) == os.getpid()


async def test_counts_match_what_each_mode_streams(client, stubs):
    # why: the page shows "(n) | (N)" and "k of N" from /api/counts before any frame arrives, so they must equal
    # the header's n_expected in mode all and in mode pass; a mismatch would show a wrong total on screen.
    c = (await client.get("/api/counts", params={k: Q[k] for k in ("ra", "dec", "size", "det")})).json()
    assert stubs.fetch_calls == 0  # index only: no S3 reads
    n_all = lines(await client.get("/api/sequence", params={**Q, "mode": "all"}))[1]["n_expected"]
    n_pass = lines(await client.get("/api/sequence", params={**Q, "mode": "pass"}))[1]["n_expected"]
    assert (c["n_exposures"], c["n_passes"]) == (n_all, n_pass)
    assert c["n_exposures"] == len(MJDS) and c["n_passes"] >= 1


async def test_counts_no_coverage_is_zero_not_error(client, monkeypatch):
    async def none(ra, dec, det):
        return []

    monkeypatch.setattr(index, "find_images", none)
    r = await client.get("/api/counts", params={k: Q[k] for k in ("ra", "dec", "size", "det")})
    assert r.status_code == 200 and r.json() == {"det": DET, "n_exposures": 0, "n_passes": 0}


async def test_counts_missing_index_is_503_and_bad_params_422(client, monkeypatch):
    async def boom(ra, dec, det):
        raise FileNotFoundError("idx")

    monkeypatch.setattr(index, "find_images", boom)
    base = {k: Q[k] for k in ("ra", "dec", "size", "det")}
    assert (await client.get("/api/counts", params=base)).status_code == 503
    assert (await client.get("/api/counts", params={**base, "size": 0.33})).status_code == 422
