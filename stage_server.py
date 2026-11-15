"""Lightweight FastAPI bridge for the Svelte stage frontend, backed by AWS EC2 (/api/cube) and drift/."""
from __future__ import annotations

import functools
import json
import os
import warnings
from concurrent.futures import ThreadPoolExecutor

from astropy.utils.exceptions import AstropyWarning
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from drift import pipeline
from drift.demo import MID_EPOCH
from drift.naming import name_field
from drift.stage_data import build_stage
from drift.targets import PRESETS, target_position

warnings.simplefilter("ignore", AstropyWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE_DIR = os.path.join(HERE, "data", "stage")

app = FastAPI(title="Draconid Drift Stage Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@functools.lru_cache(maxsize=64)
def _cached_stage_by_key(
    key: str,
    name: str,
    ra: float,
    dec: float,
    pmra: float,
    pmdec: float,
    epoch: float,
    fov_arcsec: float,
    detector: str,
) -> dict:
    """Stage payload for a spec: precomputed file for presets, else computed once and cached."""
    f = os.path.join(STAGE_DIR, f"{key}.json")
    if key in PRESETS and os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
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
    res = pipeline.run(spec, mode="target", n_null=60, build=False, max_frames=60)
    ra_now, dec_now = pipeline.propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)
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


def _cached_stage(spec: pipeline.Spec) -> dict:
    return _cached_stage_by_key(
        spec.key,
        spec.name,
        float(spec.ra),
        float(spec.dec),
        float(spec.pmra),
        float(spec.pmdec),
        float(spec.epoch),
        float(spec.fov_arcsec),
        str(spec.detector),
    )


@app.get("/api/presets")
def api_presets():
    presets = []
    for t in PRESETS.values():
        ra, dec = target_position(t, MID_EPOCH)
        presets.append(
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
    return {"presets": presets, "mid": float(MID_EPOCH)}


@app.get("/api/stage")
def api_stage(
    key: str = Query("custom"),
    ra: float = Query(0.0),
    dec: float = Query(0.0),
    fov: float = Query(180.0),
    pmra: float = Query(0.0),
    pmdec: float = Query(0.0),
    epoch: float = Query(2000.0),
    name: str = Query("Selected area"),
    detector: str = Query("SPHEREx-D3"),
    nonce: str = Query(""),
):
    name_job = None
    if key in PRESETS:
        spec = pipeline.spec_from_target(PRESETS[key])
    else:
        fov_clamped = min(max(float(fov or 180.0), 60.0), 5400.0)
        req_name = name
        if req_name in (None, "", "Selected area", "Selected patch"):
            ex = ThreadPoolExecutor(1)
            name_job = ex.submit(
                name_field,
                round(float(ra), 4),
                round(float(dec), 4),
                round(fov_clamped / 3600.0, 4),
            )
            ex.shutdown(wait=False)
            req_name = "Selected area"
        spec = pipeline.Spec(
            name=str(req_name)[:60],
            ra=float(ra),
            dec=float(dec),
            pmra=float(pmra or 0.0),
            pmdec=float(pmdec or 0.0),
            epoch=float(epoch or 2000.0),
            fov_arcsec=fov_clamped,
            detector=detector or "SPHEREx-D3",
        )
        spec = pipeline.framed(spec)

    try:
        payload = dict(_cached_stage(spec), nonce=nonce)
        if key not in PRESETS and name_job is not None:
            try:
                payload["name"] = name_job.result(timeout=0.5)
            except Exception:
                pass
        return payload
    except pipeline.DataFetchError as e:
        return JSONResponse({"error": f"No SPHEREx images here yet ({e}). Try another spot."}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"Something went wrong ({type(e).__name__}: {e})."}, status_code=500)
