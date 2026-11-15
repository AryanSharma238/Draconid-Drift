"""L2 stack cache (contract C7): gzip'd NDJSON files, atomic writes. L1 lives in fetch.py.

S3 / CloudFront lookups (Task 04 A2 `s3_url`) are CUT by SPRINT; only L2 exists.
"""

import gzip
import logging
import os
import tempfile
from pathlib import Path
from typing import IO

from backend import config
from backend.models import Grid

log = logging.getLogger("cache")

# Not in config.py (Track A owns it): ask to move.
L2_FILE_MODE = 0o644


def stack_key(grid: Grid, det: int, mode: str, data_version: str) -> str:  # contract C7
    return f"{data_version}/{grid.ra:.3f}_{grid.dec:+.3f}_{grid.size_deg}_{det}_{mode}"


def l2_path(key: str) -> Path:
    return config.STACK_CACHE_DIR / f"{key}.ndjson.gz"


def l2_get(key: str) -> Path | None:
    p = l2_path(key)
    return p if p.is_file() else None


class L2Writer:
    """Streams lines into `<key>.ndjson.gz.<random>.tmp`; commit() renames atomically, abort() deletes.

    The tmp name is unique per writer so two concurrent identical requests cannot corrupt each other.
    """

    def __init__(self, key: str) -> None:
        self.path = l2_path(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        self._tmp = Path(tmp)
        raw: IO[bytes] = os.fdopen(fd, "wb")
        self._raw = raw
        self._gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=config.L2_GZIP_LEVEL, mtime=0)
        self._done = False

    def write(self, line: bytes) -> None:
        self._gz.write(line)

    def commit(self) -> Path:
        self._close()
        self._tmp.chmod(
            L2_FILE_MODE
        )  # mkstemp makes 0600; the cache is rsynced to a box and read by another user
        os.replace(self._tmp, self.path)
        self._done = True
        return self.path

    def abort(self) -> None:
        if self._done:
            return
        self._close()
        self._tmp.unlink(missing_ok=True)
        self._done = True

    def _close(self) -> None:
        if not self._gz.closed:
            self._gz.close()
        if not self._raw.closed:
            self._raw.close()


def l2_put_stream(key: str) -> L2Writer:
    return L2Writer(key)


# --- JSON results (motion, lightcurve): STACK_CACHE_DIR/{kind}/{stack_key}.json, C7 / Task 08 M6 ---
def json_key(kind: str, grid: Grid, det: int, data_version: str) -> str:
    """Derived from the mode="all" stack_key: these results are computed from the same frames."""
    return f"{kind}/{stack_key(grid, det, 'all', data_version)}"


def json_path(key: str) -> Path:
    return config.STACK_CACHE_DIR / f"{key}.json"


def json_get(key: str) -> bytes | None:
    p = json_path(key)
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None


def json_put(key: str, body: bytes) -> Path:
    """Atomic write (tmp + os.replace), same permissions as the L2 stacks."""
    path = json_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(tmp, L2_FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def round_pm(pm_masyr: float) -> float:
    """PM as kept in a spectrum key; +0.0 turns -0.0 into 0.0 (they would format differently)."""
    return round(pm_masyr, config.PM_KEY_DECIMALS) + 0.0


def spectrum_key(grid: Grid, pmra: float, pmdec: float, data_version: str) -> str:
    """Per-position, all six detectors, so no det/mode; the (rounded) PM is part of the key: it moves the aperture."""
    return (
        f"spectrum/{data_version}/{grid.ra:.3f}_{grid.dec:+.3f}_{grid.size_deg}"
        f"_pm{round_pm(pmra):.{config.PM_KEY_DECIMALS}f}_{round_pm(pmdec):.{config.PM_KEY_DECIMALS}f}"
    )
