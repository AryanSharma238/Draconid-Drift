"""obs_id parsing and dedupe (contract C2, Task 02 I1).

FILENAME_RE was verified by Track A against 56 real S3-mirror filenames (DR1/QR2/QR3, incl. `l2b_retry`
variants) on 2026-11-14. NOT verified: the TAP `uri` path layout (IRSA TAP was down), so `release` is an
input to parse_uri and is never derived from the uri here. [deferred: TAP uri format]
"""

import re

from backend.models import ImageRef

FILENAME_RE = re.compile(
    r"^level2_(?P<exposure_id>\d{4}W\d{2}_\d[A-Z]_\d{4}_\d)D(?P<det>[1-6])_spx_"
    r"(?P<version>l2b(?P<retry>_retry)?-v(?P<vnum>\d+)-(?P<year>\d{4})-(?P<doy>\d{3}))\.fits$"
)
RELEASE_RE = re.compile(r"^(?P<kind>DR|QR)(?P<num>\d+)$")


def parse_uri(uri: str, t_mjd: float, release: str) -> ImageRef:
    """Build an ImageRef from an artifact uri. Raises ValueError (naming the uri) if the filename
    does not match FILENAME_RE. The query string, if any, is dropped (C2: uri has none)."""
    path = uri.split("?", 1)[0]
    m = FILENAME_RE.match(path.rsplit("/", 1)[-1])
    if m is None:
        raise ValueError(f"unparseable SPHEREx filename in uri: {uri!r}")
    det = int(m["det"])
    return ImageRef(
        obs_id=f"{m['exposure_id']}_D{det}",
        exposure_id=m["exposure_id"],
        detector=det,
        t_mjd=t_mjd,
        release=release,
        version=m["version"],
        uri=path,
    )


def release_rank(release: str) -> tuple[int, int]:
    """DR* beats QR*; within a family the higher number wins (DR1 > QR3 > QR2). Raises on other strings."""
    m = RELEASE_RE.match(release)
    if m is None:
        raise ValueError(f"unknown release {release!r} (expected DRn or QRn)")
    return (m["kind"] == "DR", int(m["num"]))


def version_key(version: str) -> tuple[int, int, int, int]:
    """(vNN, year, day-of-year, is_retry) compared numerically: 'v9' < 'v10', and a retry only wins a tie.
    Plain string comparison is wrong here ('_' sorts after '-')."""
    m = FILENAME_RE.match(f"level2_0000W00_0A_0000_0D1_spx_{version}.fits")
    if m is None:
        raise ValueError(f"unparseable version {version!r}")
    return int(m["vnum"]), int(m["year"]), int(m["doy"]), int(m["retry"] is not None)


def dedupe(refs: list[ImageRef]) -> list[ImageRef]:
    """Per obs_id keep the best release (DR1 > QR3 > QR2), then the highest version_key.
    Returns the survivors sorted by t_mjd."""
    best: dict[str, ImageRef] = {}
    for r in refs:
        cur = best.get(r.obs_id)
        if cur is None or (release_rank(r.release), version_key(r.version)) > (
            release_rank(cur.release),
            version_key(cur.version),
        ):
            best[r.obs_id] = r
    return sorted(best.values(), key=lambda r: r.t_mjd)
