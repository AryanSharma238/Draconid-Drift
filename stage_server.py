"""Draconid Drift Stage Server: FastAPI backend replacing Streamlit.

Serves the Svelte UI (from frontend/dist) and exposes the stage API:
  GET  /api/stage/init    -> presets + MID_EPOCH + EC2 tunnel status
  GET  /api/stage/health  -> EC2 SSH tunnel health check
  POST /api/stage/go      -> run pipeline / load stage payload + start background bands
  POST /api/stage/bands   -> all 6 detector bands + spectra for the current field
  POST /api/stage/depth   -> 3D distance & velocity catalogue for Past & future
"""
from __future__ import annotations

import hashlib
import json
import os
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

from astropy.utils.exceptions import AstropyWarning
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from drift import pipeline
from drift.demo import MID_EPOCH
from drift.stage_data import build_stage
from drift.targets import PRESETS, target_position

warnings.simplefilter("ignore", AstropyWarning)

HERE = Path(__file__).resolve().parent
STAGE_DIR = HERE / "data" / "stage"
FRONTEND_DIST = HERE / "frontend" / "dist"
CATALOG_JSON = HERE / "web" / "stage" / "catalog.json"

_NEO: dict = {}
app = FastAPI(title="Draconid Drift Stage Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background worker pools
_BAND_JOBS: dict[tuple, Any] = {}
_BAND_EX = ThreadPoolExecutor(max_workers=2)
_NAME_EX = ThreadPoolExecutor(max_workers=2)


def _build_presets() -> list[dict[str, Any]]:
    out = []
    for t in PRESETS.values():
        ra, dec = target_position(t, MID_EPOCH)
        out.append(
            dict(
                key=t.key,
                name=t.name,
                ra=float(ra),
                dec=float(dec),
                pmra=t.pmra,
                pmdec=t.pmdec,
                epoch=float(MID_EPOCH),
                fov=float(t.fov_arcsec),
                detector=t.detector,
            )
        )
    return out


@lru_cache(maxsize=64)
def _cached_stage(
    key: str,
    name: str,
    ra: float,
    dec: float,
    pmra: float,
    pmdec: float,
    epoch: float,
    fov_arcsec: float,
    detector: str,
) -> dict[str, Any]:
    """Stage payload for a target spec: precomputed JSON for presets, else live pipeline."""
    pre = STAGE_DIR / f"{key}.json"
    if key in PRESETS and pre.exists():
        with open(pre, encoding="utf-8") as fh:
            return json.load(fh)
    spec = pipeline.Spec(
        name=name,
        ra=ra,
        dec=dec,
        pmra=pmra,
        pmdec=pmdec,
        epoch=epoch,
        fov_arcsec=fov_arcsec,
        detector=detector,
        key=key,
    )
    if key in PRESETS:
        spec = pipeline.spec_from_target(PRESETS[key])
    else:
        spec = pipeline.framed(spec)
    res = pipeline.run(spec, mode="target", n_null=24, build=False, max_frames=24)
    ra_now, dec_now = pipeline.propagate(
        spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH
    )
    return build_stage(
        res,
        spec.name,
        float(ra_now),
        float(dec_now),
        spec.pmra,
        spec.pmdec,
        MID_EPOCH,
        ra0=spec.ra,
        dec0=spec.dec,
        epoch0=spec.epoch,
    )


def _band_args(r: dict[str, Any], key: str | None) -> tuple:
    tg = [
        (
            "target",
            float(r["ra"]),
            float(r["dec"]),
            float(r.get("pmra") or 0),
            float(r.get("pmdec") or 0),
            float(r.get("epoch") or MID_EPOCH),
        )
    ] + [
        (
            str(i),
            float(o["ra"]),
            float(o["dec"]),
            float(o.get("pmra") or 0),
            float(o.get("pmdec") or 0),
            float(o.get("epoch") or MID_EPOCH),
        )
        for i, o in enumerate(r.get("objects") or [])
    ]
    return (
        str(key or r["name"]),
        float(r["ra_c"]),
        float(r["dec_c"]),
        float(r["field_deg"]),
        r.get("detector") or "SPHEREx-D3",
        tuple(tg),
    )


@lru_cache(maxsize=32)
def _compute_bands(
    name: str,
    ra_c: float,
    dec_c: float,
    field_deg: float,
    det: str,
    targets: tuple,
) -> dict[str, Any]:
    from drift.bands import all_bands

    key = "bands_" + hashlib.md5(f"{ra_c:.5f}{dec_c:.5f}{field_deg:.5f}".encode()).hexdigest()[:10]
    pre = STAGE_DIR / f"{name}_bands.json"
    if name in PRESETS and pre.exists():
        with open(pre, encoding="utf-8") as fh:
            return json.load(fh)
    spec = pipeline.Spec(
        name=name,
        ra=ra_c,
        dec=dec_c,
        pmra=0.0,
        pmdec=0.0,
        epoch=float(MID_EPOCH),
        fov_arcsec=field_deg * 3600.0,
        detector=det,
        key=key,
    )
    return all_bands(spec, targets=targets)


def _start_bands(r: dict[str, Any], key: str | None):
    a = _band_args(r, key)
    if a not in _BAND_JOBS:
        if len(_BAND_JOBS) > 16:
            _BAND_JOBS.clear()
        _BAND_JOBS[a] = _BAND_EX.submit(_compute_bands, *a)
    return _BAND_JOBS[a]


@lru_cache(maxsize=32)
def _cached_depth(ra_c: float, dec_c: float, field_deg: float) -> dict[str, Any]:
    from drift.depth import depth_map

    return depth_map(ra_c, dec_c, field_deg)


class GoRequest(BaseModel):
    nonce: str | None = None
    key: str | None = "custom"
    name: str | None = "Selected area"
    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float = 2000.0
    fov: float = 180.0
    detector: str = "SPHEREx-D3"


class BandsRequest(BaseModel):
    key: str | None = None
    name: str
    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float | None = None
    ra_c: float
    dec_c: float
    field_deg: float
    detector: str = "SPHEREx-D3"
    objects: list[dict[str, Any]] = []


class DepthRequest(BaseModel):
    ra_c: float
    dec_c: float
    field_deg: float


@app.get("/api/stage/init")
def api_init() -> dict[str, Any]:
    return {
        "presets": _build_presets(),
        "mid": float(MID_EPOCH),
        "ec2": pipeline.ec2_health(timeout=0.8),
    }


@app.get("/api/stage/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "ec2": pipeline.ec2_health(timeout=1.5)}


@app.post("/api/stage/go")
def api_go(req: GoRequest) -> JSONResponse:
    key = req.key or "custom"
    name_job = None
    if key in PRESETS:
        t = PRESETS[key]
        spec_args = (
            t.key,
            t.name,
            float(t.ra),
            float(t.dec),
            float(t.pmra),
            float(t.pmdec),
            float(t.epoch),
            float(t.fov_arcsec),
            t.detector,
        )
    else:
        fov = min(max(float(req.fov or 180.0), 60.0), 18000.0)
        req_name = req.name
        if req_name in (None, "", "Selected area", "Selected patch"):
            from drift.naming import name_field

            name_job = _NAME_EX.submit(
                name_field,
                round(float(req.ra), 4),
                round(float(req.dec), 4),
                round(fov / 3600.0, 4),
            )
            req_name = "Selected area"
        spec_args = (
            f"custom_{round(float(req.ra), 5)}_{round(float(req.dec), 5)}_{round(fov, 1)}",
            str(req_name)[:60],
            float(req.ra),
            float(req.dec),
            float(req.pmra or 0.0),
            float(req.pmdec or 0.0),
            float(req.epoch or 2000.0),
            fov,
            req.detector or "SPHEREx-D3",
        )
    try:
        payload = dict(_cached_stage(*spec_args))
        payload["nonce"] = req.nonce
        if key not in PRESETS and name_job is not None:
            try:
                resolved = name_job.result(timeout=0.5)
                if resolved:
                    payload["name"] = resolved
            except Exception:
                pass
        try:
            from drift import library as _lib
            _lib.record(payload, key if key in PRESETS else None, float(req.fov or 0) or None)
        except Exception:
            pass
        try:
            _start_bands(payload, key)
        except Exception:
            pass
        if payload.get("lc") and not payload["lc"].get("pts"):      # too bright for SPHEREx: ask NEOWISE alongside
            try:
                from drift.neowise import neowise_lc
                _NEO[payload["name"]] = _NAME_EX.submit(neowise_lc, float(payload["ra"]), float(payload["dec"]),
                                                        float(payload.get("pmra") or 0), float(payload.get("pmdec") or 0),
                                                        float(payload.get("epoch") or MID_EPOCH))
            except Exception:
                pass
        return JSONResponse(payload)
    except pipeline.DataFetchError as e:
        return JSONResponse(
            {"error": f"No SPHEREx images here yet ({e}). Try another spot."},
            status_code=404,
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Something went wrong ({type(e).__name__}: {e})."},
            status_code=500,
        )


@app.post("/api/stage/bands")
def api_bands(req: BandsRequest) -> JSONResponse:
    try:
        r = req.model_dump()
        a = _band_args(r, req.key)
        fut = _BAND_JOBS.get(a)
        if fut is not None:
            b = fut.result()
        else:
            b = _compute_bands(*a)
        out = {"bands": b.get("bands", []), "spectra": b.get("spectra", {})}
        lc2 = None
        fut = _NEO.pop(r.get("name"), None)
        if fut is not None:
            try:
                lc2 = fut.result(timeout=30)
            except Exception:
                lc2 = None
            if lc2:
                out["lc"] = lc2
        try:
            from drift import library as _lib
            _lib.record_bands(r.get("name"), b, lc2)
        except Exception:
            pass
        return JSONResponse(out)
    except Exception:
        traceback.print_exc()
        return JSONResponse({"bands": [], "spectra": {}})


@app.get("/api/stage/library")
def api_library() -> JSONResponse:
    from drift import library as _lib
    d = _lib.listing()
    return JSONResponse(dict(discoveries=d["discoveries"][:80], library=d["library"][:120]))


@app.post("/api/stage/depth")
def api_depth(req: DepthRequest) -> JSONResponse:
    try:
        d = _cached_depth(
            round(float(req.ra_c), 5),
            round(float(req.dec_c), 5),
            round(float(req.field_deg), 5),
        )
        return JSONResponse({"depth": d})
    except Exception:
        traceback.print_exc()
        return JSONResponse(
            {
                "depth": {
                    "objects": [],
                    "error": "The distance catalogues did not answer. Try again in a moment.",
                }
            }
        )


@app.get("/catalog.json")
def serve_catalog():
    if (FRONTEND_DIST / "catalog.json").exists():
        return FileResponse(FRONTEND_DIST / "catalog.json")
    if CATALOG_JSON.exists():
        return FileResponse(CATALOG_JSON)
    raise HTTPException(status_code=404, detail="catalog.json not found")


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("stage_server:app", host="0.0.0.0", port=8501, reload=False)

