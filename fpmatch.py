"""
Python wrapper for nbis/libfpmatch.dylib: NIST NBIS minutiae extraction
(mindtct) and matching (bozorth3), the same matcher libfprint uses.
"""

import ctypes
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(HERE, "nbis", "libfpmatch.dylib")

MAX_MINUTIAE = 200
PPMM = 19.685  # 500 dpi, as libfprint assumes for this reader

# libfprint's default bozorth3 threshold: scores at or above this are a match.
MATCH_THRESHOLD = 40

if not os.path.exists(LIB_PATH):
    raise ImportError(f"{LIB_PATH} not found; build it with: sh nbis/build.sh")

_lib = ctypes.CDLL(LIB_PATH)
_IntArr = ctypes.c_int * MAX_MINUTIAE
_int_p = ctypes.POINTER(ctypes.c_int)

_lib.fp_extract.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_double,
                            _int_p, _int_p, _int_p]
_lib.fp_extract.restype = ctypes.c_int
_lib.fp_match.argtypes = [ctypes.c_int, _int_p, _int_p, _int_p,
                          ctypes.c_int, _int_p, _int_p, _int_p]
_lib.fp_match.restype = ctypes.c_int


def extract(pixels, width, height):
    """Return minutiae as {"x": [...], "y": [...], "t": [...]}.

    pixels: 8-bit greyscale bytes, dark ridges on a light background.
    """
    if len(pixels) != width * height:
        raise ValueError("pixel buffer does not match image size")
    xs, ys, ts = _IntArr(), _IntArr(), _IntArr()
    n = _lib.fp_extract(bytes(pixels), width, height, PPMM, xs, ys, ts)
    if n < 0:
        raise RuntimeError(f"Minutiae extraction failed (code {n})")
    return {"x": list(xs[:n]), "y": list(ys[:n]), "t": list(ts[:n])}


def _arrays(m):
    n = min(len(m["x"]), MAX_MINUTIAE)
    return n, _IntArr(*m["x"][:n]), _IntArr(*m["y"][:n]), _IntArr(*m["t"][:n])


def score(probe, gallery):
    """bozorth3 similarity score between two minutiae sets (higher is more similar)."""
    score = _lib.fp_match(*_arrays(probe), *_arrays(gallery))
    if score < 0:
        raise RuntimeError("Matching failed")
    return score
