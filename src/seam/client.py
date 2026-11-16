"""The canonical client: exactly what a binding needs, and nothing else.

This file exists because of a specific failure. With three bindings written independently, each
one had quietly invented things the manifest did not say: how to group controls, how many digits
to show, whether a trigger fires on one keypress or needs arming, what a slider position means
on a log scale. Four bindings, four sets of answers, and no way to notice until somebody
compared two windows side by side.

So the client is the shared answer. A binding that uses it inherits the normative formatting,
grouping, and commit rules; a binding that reimplements it (the JS one, of necessity) has a
checklist to match, and `tests/test_bindings.py` compares the two.

Deliberately stdlib-only. A binding may pull in ImGui, Qt, or Chromium; the *answers* must not
depend on which.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_PRECISION = 4


# --------------------------------------------------------------------------
# formatting — SPEC 4.7
# --------------------------------------------------------------------------


def format_value(value: Any, precision: int | None = None) -> str:
    """Render a value the way every binding must render it (SPEC 4.7).

    The rule is written numerically rather than by type, because JSON erases the difference
    between ``200`` and ``200.0`` before any binding sees it. Two implementations that
    disagreed about that distinction rendered the same sample as ``1234567`` and ``1.235e6``.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if not isinstance(value, (int, float)):
        return str(value)

    v = float(value)
    if not math.isfinite(v):
        return str(v)

    p = DEFAULT_PRECISION if precision is None else int(precision)
    if p == 0:
        return str(int(round(v)))
    if v == 0:
        return "0"

    integral = float(v).is_integer() and abs(v) < 1e18
    if integral:
        digits = len(str(abs(int(v))))
        if digits <= p:
            return str(int(v))

    magnitude = math.floor(math.log10(abs(v)))
    if magnitude >= p or magnitude < -2:
        # Trailing zeros in a scientific mantissa are *precision information* and are kept:
        # "4.00e-10" says three digits, "4e-10" says one.
        mantissa, exponent = f"{v:.{max(0, p - 1)}e}".split("e")
        return f"{mantissa}e{int(exponent)}"

    decimals = max(0, p - 1 - magnitude)
    if decimals == 0:
        return f"{v:.0f}"
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


def display_value(point: dict, value: Any) -> str:
    """Format with the point's declared precision, appending its unit."""
    text = format_value(value, point.get("precision"))
    unit = point.get("unit")
    return f"{text} {unit}" if unit else text


def group_points(controls: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group in manifest order, using each point's declared ``group``.

    A control with no group goes into the empty-string section, which every binding renders
    last. Grouping is *not* a binding decision: the web surface used to group by liveness,
    the terminal one by affordance, and both were inventing structure the manifest did not have.
    """
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for c in controls:
        g = c.get("group") or ""
        if g not in buckets:
            buckets[g] = []
            order.append(g)
        buckets[g].append(c)
    # First-appearance order, not alphabetical: manifest order is presentation order (SPEC 3.3),
    # and a sort here would silently reshuffle every surface that trusted it. Ungrouped last.
    order.sort(key=lambda g: g == "")
    return [(g, buckets[g]) for g in order]


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


@dataclass
class FrameData:
    rgb: bytes
    width: int
    height: int
    meta: dict


class SeamError(RuntimeError):
    pass


class SeamClient:
    def __init__(self, base: str, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.last_error: str | None = None

    # -- reads ---------------------------------------------------------
    def _get_json(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return json.loads(r.read())

    def manifest(self) -> dict:
        return self._get_json("/api/manifest")

    def state(self) -> dict:
        return self._get_json("/api/state")

    def scenarios(self) -> list[dict]:
        return self._get_json("/api/scenarios")

    # -- writes --------------------------------------------------------
    def _post(self, path: str, body: dict) -> Any:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def intervene(self, point: str, value: Any) -> dict:
        return self._post("/api/intervene", {"point": point, "value": value})

    def stop(self) -> dict:
        return self._post("/api/stop", {})

    def bench(self, point: str, patch: dict) -> dict:
        return self._post("/api/bench", {"point": point, "patch": patch})

    # -- frames --------------------------------------------------------
    def frame(self, point: str, width: int = 120) -> FrameData | None:
        url = f"{self.base}/api/frame?point={point}&w={width}&format=rgba"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                body = r.read()
                w = int(r.headers.get("X-Seam-Width", "0"))
                h = int(r.headers.get("X-Seam-Height", "0"))
                meta = json.loads(r.headers.get("X-Seam-Frame", "{}"))
            if w * h * 3 != len(body):
                return None
            self.last_error = None
            return FrameData(body, w, h, meta)
        except urllib.error.HTTPError as e:
            try:
                self.last_error = json.loads(e.read()).get("error", str(e))
            except Exception:  # noqa: BLE001
                self.last_error = str(e)
            return None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return None

    def first_frame(self, grid_names: list[str], width: int = 120) -> tuple[str, FrameData] | None:
        """Try each grid point until one produces a frame.

        A binding must not dead-end on the first declared grid observation: the first one in
        manifest order may be an on-demand artifact nobody has produced yet.
        """
        for name in grid_names:
            fr = self.frame(name, width)
            if fr is not None:
                return name, fr
        return None


# --------------------------------------------------------------------------
# commit boundary — SPEC 4.7
# --------------------------------------------------------------------------


class Draft:
    """A pending edit that has not crossed the seam yet.

    Bindings with continuous input devices (a slider being dragged, a key being held) must
    coalesce adjustments into one submission. Without this rule, a web slider drag and a terminal
    key-repeat produce the same user intent and two very different traces: one crossing versus
    forty.
    """

    def __init__(self) -> None:
        self.point: str | None = None
        self.value: Any = None
        self.dirty = False

    def set(self, point: str, value: Any) -> None:
        self.point = point
        self.value = value
        self.dirty = True

    def clear(self) -> None:
        self.point = None
        self.dirty = False

    def commit(self, client: SeamClient, actor_note: str = "") -> dict | None:
        if not self.dirty or self.point is None:
            return None
        r = client.intervene(self.point, self.value)
        if actor_note and isinstance(r, dict):
            r.setdefault("note", actor_note)
        self.clear()
        return r
