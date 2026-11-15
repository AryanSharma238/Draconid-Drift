from pathlib import Path

# --- paths ---
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
INDEX_DB = DATA_DIR / "index" / "spherex.sqlite"
RAW_CACHE_DIR = DATA_DIR / "cache" / "raw"  # L1
STACK_CACHE_DIR = DATA_DIR / "cache" / "stacks"  # L2

# --- IRSA ---
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
IRSA_TAP_URL = "https://irsa.ipac.caltech.edu/TAP"
# CONFIRMED 2026-11-14: anonymous ListObjectsV2 works; x-amz-bucket-region header = us-east-1.
# Key layout: {dr1|qr2|qr3}/level2/{week}_{block}/{version}/{det}/{filename}. Files are uncompressed FITS.
SPHEREX_S3_BUCKET = "nasa-irsa-spherex"
SPHEREX_S3_REGION = "us-east-1"
FETCH_METHOD = "s3"  # "cutout" | "s3". SPRINT decision 2026-11-14: IRSA cutout service down (503)

# --- instrument ---
PIXSCALE_ARCSEC = (
    6.15  # CONFIRMED 2026-11-14: Expl. Supplement v2.1 "6.15″ × 6.15″"; D6 header PC matrix 6.157
)
DETECTORS = (1, 2, 3, 4, 5, 6)
BANDPASS_NAME = "SPHEREx-D{det}"  # value of p.energy_bandpassname

# --- flags (bit numbers in FLAGS HDU) ---
# CONFIRMED 2026-11-14: SPHEREx Explanatory Supplement v2.1 Table 16; matches the MSKMnnnn cards in a real
# DR1 D6 FLAGS header. Bits 3-5, 8, 13, 16, 18 are unused.
FLAG_BIT_NAMES = {
    0: "TRANSIENT", 1: "OVERFLOW", 2: "SUR_ERROR", 6: "NONFUNC", 7: "DICHROIC", 9: "MISSING_DATA",
    10: "HOT", 11: "COLD", 12: "FULLSAMPLE", 14: "PHANMISS", 15: "NONLINEAR", 17: "PERSIST",
    19: "OUTLIER", 20: "CROSSTALK", 21: "SOURCE", 22: "GHOST", 23: "GHOST_FPA", 24: "GHOST_EXT",
    25: "STREAK", 26: "BLOOM", 27: "SNOWBALL", 28: "HALO", 29: "SATELLITE_HALO",
}  # fmt: skip
# Track A decision: mask unusable pixels and artifacts that would blink as false movers.
# NOT masked: OVERFLOW/NONLINEAR/BLOOM/HALO (bright star cores, e.g. Barnard's itself),
# SOURCE (47.6% of pixels in the sample frame), GHOST* (large areas), FULLSAMPLE/CROSSTALK (informational).
# 2026-11-14 (docs/flag_mask_experiments.md): TRANSIENT (0) and SUR_ERROR (2) were REMOVED from the mask. On
# bright stars the pipeline sets them on the star's own core and wings (Barnard's: 55 of 58 frames had the
# aperture masked; raw pixels are finite). With them unmasked: M5 still 4/4 (Barnard D3 false candidates 6 -> 0),
# Barnard's full-aperture frames 3 -> 50 of 58, RW Aur and eps Ind B spectra cache. OPEN: the Explanatory
# Explanatory Supplement v1.0 (SSDC-DP-017) Table 9 p.26 and p.22, READ 2026-11-14: TRANSIENT = a transient such as a
# cosmic ray detected during the ramp, ALSO charge spillover (bloom) from very bright neighbours; SUR_ERROR = onboard
# processing consistency bit (with TRANSIENT/OVERFLOW: an EARLY transient/overflow). Their pixels are NOT invalid but
# less accurate: TRANSIENT pixels get an ESTIMATED nonlinearity correction, SUR_ERROR pixels get NONE (so flux is
# biased low where nonlinear). The frontend marks such spectrum points (hollow) instead of hiding them.
# Still shown in the flag overlay and metadata line (frontend), just not blanked.
FLAG_BITS_BAD = (6, 7, 9, 10, 11, 14, 17, 19, 25, 27)

# --- processing ---
SUBTRACT_ZODI = True  # CONFIRMED 2026-11-14: data-products doc "has not been subtracted"; D6 median IMAGE 0.39 ≈ ZODI 0.33
PASS_GAP_DAYS = 30.0  # gap that starts a new survey pass
ASINH_A = 0.1  # default asinh softening
ZSCALE_MAX_SAMPLES = 100_000

# --- request limits ---
FOV_CHOICES_DEG = (0.05, 0.1, 0.25, 0.5)
DEFAULT_FOV_DEG = 0.1
MAX_FOV_DEG = 0.5
COORD_SNAP_DEG = 0.001  # snap request centre for cache sharing (3.6″ < 1 px)

# --- fetching ---
FETCH_CONCURRENCY = 48  # global, across all requests. 2026-11-14 EC2 A/B: 32 ≈ 64 > 16 (with S3_READ_WORKERS)
FETCH_RETRIES = 3
FETCH_TIMEOUT_S = 30.0
PROCESS_WORKERS = 8
# S3 box reads run in their own process pool: in threads, astropy header/WCS parsing serialises on the GIL
# (2026-11-14 EC2 A/B, fresh fields: threads 6.3 frames/s, first frame 1.7-3.2 s; processes 13.7/s, 0.9 s).
S3_READ_WORKERS = 24
PASS_PREVIEW_FRAMES = 4  # C5 pass previews: send a pass co-add early once this many of its inputs arrived
S3_BOX_PAD_PX = 3  # padding around the grid box in source pixels, for bilinear edges + SIP margin
S3_READ_BLOCK_BYTES = 262_144  # fallback GET on a prefetch miss (5 MB blocks made one frame ~38 s)
S3_GET_PARALLEL = 8  # parallel ranged GETs per frame read (inside each read worker process)
S3_EDGE_SAMPLES = 5  # grid-edge samples per axis used to find the source-pixel bounding box
# E4 light-curve photometry (tasks/09-edge.md E4), px
LC_APERTURE_R = 2.0
LC_ANNULUS_R_IN = 4.0
LC_ANNULUS_R_OUT = 6.0
LC_MIN_TEMPLATE_SNR = 3.0  # skip a point when f_t <= this * e_t
# PSF-wing ("ring") photometry for saturated cores (2026-11-14): R Dra's delivered pixels are NaN out to ~2 px, finite
# from 3 px (median ~106 counts/px at r=3 vs ~4 at r=10). When fewer than RING_AUTO_APERTURE_FRAC of a target's frames
# have a finite 2 px aperture, a spectrum/light curve uses the sum over the r_in..r_out annulus instead. Relative only:
# the ring holds a wavelength-dependent fraction of the total flux (the PSF broadens with wavelength).
RING_R_IN = 3.0
RING_R_OUT = 6.0
RING_MIN_FINITE_FRAC = 0.9  # skip a frame when fewer ring pixels are finite
RING_AUTO_APERTURE_FRAC = 0.5

# --- index ---
HEALPIX_NSIDE = 32  # order 5, ~1.8° cells. DO NOT use finer: footprints are ~3.5°
INDEX_MAX_AGE_DAYS = 10  # older → also query live TAP

# --- S3 footprint index (SPRINT decision 2026-11-14: IRSA outage → S3) ---
S3_PUBLIC_BASE = "https://nasa-irsa-spherex.s3.amazonaws.com/"  # anonymous HTTPS for listing + ranged GETs
S3_INDEX_PATH = DATA_DIR / "index" / "s3_footprints.csv.gz"
S3_INDEX_RELEASES = ("dr1", "qr3")  # QR2 is superseded by DR1 + QR3 except a few overlap weeks
S3_INDEX_DET = 6  # D3/D6 (and D1/D4, D2/D5) share pointing to 0.02°, so one D6 header covers D3 and D6
# requested det -> det whose headers are indexed for it. CONFIRMED 2026-11-14: CRVAL of D1/D4, D2/D5, D3/D6
# agree to 0.02° on dr1 2025W17_4B_0002_1 (all six headers read).
S3_INDEX_PAIRS = {1: 4, 4: 4, 2: 5, 5: 5, 3: 6, 6: 6}
S3_INDEX_PATHS = {
    6: S3_INDEX_PATH,
    4: DATA_DIR / "index" / "s3_footprints_D4.csv.gz",
    5: DATA_DIR / "index" / "s3_footprints_D5.csv.gz",
}
S3_HEADER_BYTES = 28800  # PRIMARY (2880 B) + IMAGE header (~22 KB) fit in 10 FITS blocks
S3_INDEX_CONCURRENCY = 64
RELEASE_PRIORITY = ("dr1", "qr3", "qr2")  # dedupe order (contract C2)

# --- stored stacks (Task 04) ---
STACKS_BUCKET = ""  # empty = disabled
STACKS_PUBLIC_BASE = ""  # CloudFront URL; empty = disabled

# --- motion (Task 08) ---
LAMBDA_MATCH_FRAC = 0.02  # |Δλ|/λ for template matching (≈ SPHEREx resolving power 40–130; tune in M5)
# CONFIRMED 2026-11-14: header PSF_FWHM = 9.04″ (D6 sample) / 8.67″ (Supplement example) → 1.41-1.47 px FWHM.
# Varies per image: the IMAGE header carries PSF_FWHM (arcsec) if a per-frame value is needed.
PSF_SIGMA_PX = 0.62
PSF_FWHM_PX = 2.355 * PSF_SIGMA_PX
PEAK_NSIG = 5.0
REG_NSIG = 15.0  # bright stars for registration
VMAX_PX_PER_YR = 4.0  # ≈ 25″/yr at 6.2″/px; faster than any known star except Barnard's-class
VGRID_STEP_PX = 0.5  # max misalignment across the baseline per grid step
MIN_V_PX_PER_YR = 0.3
MIN_MOTION_SIG = 3.0
MAX_MOVERS = 20
# ghost veto in measure_movers (added by Track D, moved here by Track A)
GHOST_FRAC = 0.5  # flux seen elsewhere / flux at the track point above which we veto
GHOST_NSIG = 3.0  # ... and only if that flux is also above this many noise sigmas
GHOST_HALF = 1  # aperture half-width in px (1 = 3x3)

# --- edge (Task 09) ---
SPHEREX_MID_EPOCH = 2025.9  # Track A decision 2026-11-14: grid-centre reference epoch. Test-target data span
# MJD 60789-61311 (2025.3-2026.7, midpoint ~2026.0); kept at 2025.9 because every precomputed stack and
# scripts/targets.py TARGET_EPOCH_JYEAR are centred on it. Changing it invalidates L2 (bump data_version).
COADD_CLIP_SIGMA = 4.0
BKG_BOX_PX = 16
VALIDATION_DET = 3  # [VERIFY]

# --- API / cache / precompute (moved from Track B modules 2026-11-14) ---
MAX_FAIL_FRAC = 0.2  # a stack with >= this fraction of failed inputs is streamed but not cached
SPECTRUM_APERTURE_R = 2.0  # F3 forced photometry, px (same radii as E4)
SPECTRUM_ANNULUS_R_IN = 4.0
SPECTRUM_ANNULUS_R_OUT = 6.0
RESOLVE_MAX_AGE_S = 86400
L2_MAX_AGE_S = (
    300  # short until the 5-target check passes: browsers must pick up stack fixes. Then 1 yr + immutable
)
WARMUP_TIMEOUT_S = 90.0  # startup waits at most this long for the pools
L2_GZIP_LEVEL = 6  # level 9 is ~3x slower for <5% smaller files
PM_KEY_DECIMALS = 1  # mas/yr kept in a spectrum cache key (0.05 mas/yr over 1 yr is far below a pixel)
LIGHTCURVE_TARGETS = ("rwaur", "rdra")  # the variables; a fixed aperture is meaningless on high-PM targets
SPECTRUM_TARGETS = ("wise0855", "barnard", "epsindb", "rwaur", "rdra")  # the 5 test targets
