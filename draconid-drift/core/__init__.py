"""Parallax core package."""
from astropy import log as _astropy_log

# SPHEREx headers trigger a benign SIP-labelling notice on every WCS build; keep logs clean.
_astropy_log.setLevel("ERROR")
