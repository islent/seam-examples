"""Frame pipeline: npy -> decimate -> colour -> PNG, with no dependencies.

Why this exists at all, and why it is stdlib-only.

A `grid` observation on a live pitch cannot be shipped whole. A 512x512 float64 slice is
2 MB; at one frame a second over SSH that is a 2 MB/s seam, which is not a seam, it is a
bandwidth bill. So the manifest declares a **preview level** and the producing side reduces
before it transports (SPEC §4.6). What crosses is a screenshot of the data, not the data —
and the full array stays available `on-demand` for the tools that actually need it.

Implemented here rather than delegated to numpy/matplotlib because the core must stay
dependency-free: a specification whose reference implementation needs a 200 MB stack is a
specification nobody checks. The binding layer (desktop, browser) may use anything it likes.
"""

from __future__ import annotations

import ast
import math
import struct
import zlib
from dataclasses import dataclass

# --------------------------------------------------------------------------
# .npy reader
# --------------------------------------------------------------------------

_TYPECODE = {
    "f8": "d", "f4": "f", "f2": None,
    "i8": "q", "i4": "i", "i2": "h", "i1": "b",
    "u8": "Q", "u4": "I", "u2": "H", "u1": "B",
    "b1": "B",
}
_ITEMSIZE = {"d": 8, "f": 4, "q": 8, "i": 4, "h": 2, "b": 1, "Q": 8, "I": 4, "H": 2, "B": 1}


def read_npy(blob: bytes) -> tuple[tuple[int, ...], list[float]]:
    """Parse a C-order, real-valued .npy into (shape, flat values).

    Supports npy format 1.0 and 2.0. Complex arrays are rejected with a message that says what
    to do instead, because silently taking the real part produces a plausible wrong picture.
    """
    import array

    if blob[:6] != b"\x93NUMPY":
        raise ValueError("not a .npy file (bad magic)")
    major = blob[6]
    if major == 1:
        hlen = struct.unpack("<H", blob[8:10])[0]
        start = 10
    elif major in (2, 3):
        hlen = struct.unpack("<I", blob[8:12])[0]
        start = 12
    else:
        raise ValueError(f"unsupported .npy version {major}")

    header = ast.literal_eval(blob[start:start + hlen].decode("latin1"))
    descr = str(header["descr"])
    shape = tuple(int(x) for x in header["shape"])
    fortran = bool(header.get("fortran_order", False))

    if descr.lstrip("<>=|").startswith("c"):
        raise ValueError(
            "complex array: save abs(field) instead, or convert on the producing side. "
            "Taking the real part silently would render a field that does not exist."
        )

    body = blob[start + hlen:]
    code = _TYPECODE.get(descr.lstrip("<>=|"))
    if code is None:
        raise ValueError(f"unsupported dtype {descr!r}")

    arr = array.array(code)
    n = 1
    for d in shape:
        n *= d
    need = n * _ITEMSIZE[code]
    arr.frombytes(body[:need])
    if descr[0] == ">" and _ITEMSIZE[code] > 1:
        arr.byteswap()

    values = arr.tolist()
    if fortran and len(shape) == 2:
        ny, nx = shape
        values = [values[x * ny + y] for y in range(ny) for x in range(nx)]
    return shape, values


# --------------------------------------------------------------------------
# decimation
# --------------------------------------------------------------------------


def decimate_2d(shape: tuple[int, ...], values: list[float], max_width: int) -> tuple[int, int, list[float]]:
    """Box-average down to at most ``max_width`` columns. Returns (h, w, flat)."""
    if len(shape) != 2:
        raise ValueError(f"frame pipeline expects a 2D grid, got shape {shape}")
    ny, nx = shape
    factor = max(1, math.ceil(max(ny, nx) / max(1, max_width)))
    oh, ow = max(1, ny // factor), max(1, nx // factor)
    out: list[float] = [0.0] * (oh * ow)
    counts = [0] * (oh * ow)
    for y in range(ny):
        oy = min(y // factor, oh - 1)
        row = y * nx
        obase = oy * ow
        for x in range(nx):
            o = obase + min(x // factor, ow - 1)
            out[o] += values[row + x]
            counts[o] += 1
    for i, c in enumerate(counts):
        if c:
            out[i] /= c
    return oh, ow, out


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

# A symmetric diverging map for fields that cross zero (Ez, potentials, residuals): sign is the
# first thing a physicist reads, so the map must encode sign and not just magnitude.
_DIVERGING = [
    (0.00, (14, 40, 120)),
    (0.25, (58, 118, 200)),
    (0.50, (246, 246, 243)),
    (0.75, (232, 136, 58)),
    (1.00, (146, 20, 18)),
]

# A sequential map for non-negative quantities (density, |E|, power): perceptually ordered,
# dark at low values so that structure appears against the panel background.
_SEQUENTIAL = [
    (0.00, (247, 247, 244)),
    (0.20, (200, 220, 210)),
    (0.40, (110, 180, 170)),
    (0.60, (40, 120, 150)),
    (0.80, (46, 60, 120)),
    (1.00, (24, 20, 56)),
]


def _lerp(cmap: list[tuple[float, tuple[int, int, int]]], t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    for i in range(len(cmap) - 1):
        a, ca = cmap[i]
        b, cb = cmap[i + 1]
        if a <= t <= b:
            f = 0.0 if b == a else (t - a) / (b - a)
            return (round(ca[0] + (cb[0] - ca[0]) * f),
                    round(ca[1] + (cb[1] - ca[1]) * f),
                    round(ca[2] + (cb[2] - ca[2]) * f))
    return cmap[-1][1]


def colorize(values: list[float], *, diverging: bool | None = None) -> tuple[bytes, dict]:
    """Map values to RGB bytes. Returns (rgb, meta) where meta records the normalisation."""
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return b"\x00" * (3 * len(values)), {"lo": 0.0, "hi": 0.0, "diverging": False}

    lo, hi = min(finite), max(finite)
    if diverging is None:
        diverging = lo < 0.0 < hi
    if diverging:
        m = max(abs(lo), abs(hi)) or 1.0
        lo, hi = -m, m
        cmap = _DIVERGING
    else:
        cmap = _SEQUENTIAL

    span = (hi - lo) or 1.0
    buf = bytearray(len(values) * 3)
    for i, v in enumerate(values):
        t = (v - lo) / span if math.isfinite(v) else 0.0
        r, g, b = _lerp(cmap, t)
        o = i * 3
        buf[o] = r
        buf[o + 1] = g
        buf[o + 2] = b
    return bytes(buf), {"lo": lo, "hi": hi, "diverging": diverging}


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ValueError("rgb buffer does not match width*height*3")
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    idat = zlib.compress(raw, 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# one call the server can use
# --------------------------------------------------------------------------


@dataclass
class Frame:
    """A reduced grid. Carries raw RGB so a native binding can upload a texture without
    decoding anything, and encodes PNG lazily for bindings that would rather have a file."""

    rgb: bytes
    width: int
    height: int
    meta: dict
    _png: bytes | None = None

    @property
    def png(self) -> bytes:
        if self._png is None:
            self._png = encode_png(self.width, self.height, self.rgb)
        return self._png

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height, **self.meta}


def make_frame(npy_blob: bytes, *, max_width: int = 480, diverging: bool | None = None) -> Frame:
    shape, values = read_npy(npy_blob)
    h, w, small = decimate_2d(shape, values, max_width)
    rgb, meta = colorize(small, diverging=diverging)
    meta["source_shape"] = list(shape)
    meta["decimated_to"] = [h, w]
    return Frame(rgb, w, h, meta)


def make_frame_from_png(png_blob: bytes) -> Frame | None:
    """If the producer already wrote a PNG, pass it through — the manifest only cares about bytes."""
    if png_blob[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png_blob[16:24])
        # A producer that already wrote a PNG is passed through as-is: the manifest promises
        # bytes on the seam, not a pixel format.
        return Frame(b"", w, h, {"passthrough": True}, _png=png_blob)
    return None
