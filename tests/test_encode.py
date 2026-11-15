import base64
import logging

import numpy as np
import orjson

from backend import encode
from backend.models import Frame, ImageRef, make_grid


def _decode_f16(s, n):
    return np.flipud(np.frombuffer(base64.b64decode(s), "<f2").reshape(n, n))


def _frame(data, obs="e_D6", t=60812.31):
    ref = ImageRef(obs, "e", 6, t, "DR1", "v1", "x")
    return Frame(ref, np.asarray(data, np.float32), None, np.zeros(np.shape(data), np.uint32), 4.5)


def test_round_trip_with_nan():
    """Client decodes with the same layout; NaN (masked pixels) must survive float16."""
    a = np.arange(25, dtype=np.float32).reshape(5, 5) / 3
    a[2, 2] = np.nan
    out = _decode_f16(encode.b64_f16(a), 5)
    assert np.isnan(out[2, 2])
    m = ~np.isnan(a)
    np.testing.assert_allclose(out[m], a[m], rtol=1e-3)


def test_orientation_row0_south_is_last_wire_row():
    """Contract C4: wire row 0 = north. Getting this wrong flips the sky."""
    a = np.zeros((5, 5), np.float32)
    a[0, 1] = 7.0  # FITS row 0 = south
    raw = np.frombuffer(base64.b64decode(encode.b64_f16(a)), "<f2").reshape(5, 5)
    assert raw[4, 1] == 7.0 and raw[0, 1] == 0.0
    u = np.zeros((5, 5), np.uint32)
    u[0, 1] = 9
    rawu = np.frombuffer(base64.b64decode(encode.b64_u32(u)), "<u4").reshape(5, 5)
    assert rawu[4, 1] == 9


def test_float16_overflow_clipped_and_logged(caplog):
    """Values past 65504 would become inf and break the client's stretch."""
    a = np.array([[1e6, -1e6], [1.0, np.nan]], np.float32)
    with caplog.at_level(logging.WARNING, logger="encode"):
        out = _decode_f16(encode.b64_f16(a), 2)
    assert np.isfinite(out[0, 0]) and np.isfinite(out[0, 1]) and np.isnan(out[1, 1])
    assert "clipping" in caplog.text


def test_lines_are_ndjson():
    """Every message ends with \\n, parses, and carries `type`; contract fields are present."""
    grid = make_grid(133.795, -7.245, 0.1)
    f = _frame(np.ones((grid.npix, grid.npix)))
    f.pass_idx = 2
    lines = [
        encode.header_line(grid, 6, "all", 3, "dv", "live", None),
        encode.header_line(grid, 6, "all", 3, "dv", "cache", (-1.0, 2.0)),
        encode.frame_line(0, f, True),
        encode.frame_line(1, f, False),
        encode.zscale_line(-0.8, 12.4),
        encode.error_line("x_D6", "timeout"),
        encode.done_line(2, 1, 100),
    ]
    objs = []
    for ln in lines:
        assert ln.endswith(b"\n") and ln.count(b"\n") == 1
        objs.append(orjson.loads(ln))
    assert [o["type"] for o in objs] == [
        "header",
        "header",
        "frame",
        "frame",
        "zscale",
        "error",
        "done",
    ]
    assert objs[0]["zscale"] is None and objs[1]["zscale"] == [-1.0, 2.0]
    assert objs[0]["npix"] == grid.npix and objs[0]["v"] == 1
    assert "flags" in objs[2] and "flags" not in objs[3]
    assert objs[2]["date"] == "2025-05-10T07:26:24Z" or objs[2]["date"].endswith("Z")
    assert objs[2]["pass"] == 2 and objs[2]["kind"] == "single" and objs[2]["det"] == 6


def test_pass_frame_kind():
    """Stored stacks put kind="pass" frames first; the client keys on it."""
    ln = encode.frame_line(0, _frame(np.zeros((3, 3)), obs="pass0_D6"), False, n_coadd=34)
    o = orjson.loads(ln)
    assert o["kind"] == "pass" and o["n_coadd"] == 34
