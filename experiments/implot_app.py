#!/usr/bin/env python3
"""ImGui desktop binding for a Seam surface.

Why a native binding at all, when the browser one already works?

Because `Binding` is the replaceable half of the specification. The manifest fixes *what may be
read and written*; a binding only decides how that looks. If only one binding exists, nobody
can tell which decisions were the specification and which were the web page. So this exists as
the second data point — and it is also the better tool for two jobs:

* **60 fps curves and field frames.** ImPlot redraws on the GPU without a DOM, and the field
  arrives as raw RGB that goes straight into a texture. No PNG decode, no canvas, no reflow.
* **Local post-processing.** A desktop process can read artifacts off the local disk directly,
  which is where a specimen or a downloaded run actually lives.

The client below is deliberately dumb: three HTTP calls and one binary fetch. Any shell —
this, Electron, a TUI — speaks exactly this and nothing more.

    python experiments/implot_app.py --url http://127.0.0.1:8770
    python experiments/implot_app.py --url http://127.0.0.1:8770 --selftest   # no window
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# client — no GUI imports, so --selftest runs anywhere
# ---------------------------------------------------------------------------


# The client is shared with every other binding (src/seam/client.py), so that formatting,
# grouping and the commit boundary are one implementation rather than three opinions.
from seam.client import Draft, FrameData, SeamClient, display_value, format_value, group_points  # noqa: E402


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

LIVENESS_COLOR = {
    "sealed": (0.55, 0.55, 0.55),
    "perstep": (0.72, 0.45, 0.05),
    "midrun": (0.10, 0.55, 0.30),
}


@dataclass
class App:
    client: SeamClient
    manifest: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    frame: FrameData | None = None
    texture: Any = None
    texture_key: tuple | None = None
    frame_point: str = ""
    selected: str = ""
    armed: set = field(default_factory=set)
    draft: Draft = field(default_factory=Draft)
    last_poll: float = 0.0
    poll_seconds: float = 0.5
    banner: str = ""
    banner_until: float = 0.0

    # -- data ----------------------------------------------------------
    def poll(self) -> None:
        now = time.time()
        if now - self.last_poll < self.poll_seconds:
            return
        self.last_poll = now
        try:
            self.manifest = self.client.manifest()
            self.state = self.client.state()
        except Exception as e:  # noqa: BLE001
            self.notify(f"surface unreachable: {e}")
            return
        grid_points = [c["name"] for c in self.state.get("controls", [])
                       if c.get("role") == "observation" and c.get("shape") == "grid"]
        if grid_points and self.frame_point not in grid_points:
            self.frame_point = grid_points[0]

    def notify(self, text: str, seconds: float = 6.0) -> None:
        self.banner = text
        self.banner_until = time.time() + seconds

    def commit(self) -> None:
        """Submit the pending draft as one crossing (SPEC 4.7c)."""
        if not self.draft.dirty:
            return
        point, value = self.draft.point, self.draft.value
        self.draft.clear()
        self.act(point, value)

    def act(self, point: str, value: Any) -> None:
        try:
            r = self.client.intervene(point, value)
        except Exception as e:  # noqa: BLE001
            self.notify(f"{point}: {e}")
            return
        outcome = r.get("outcome")
        note = r.get("note") or ""
        if outcome == "accepted":
            self.notify(f"{point} = {value!r} accepted")
        else:
            self.notify(f"{point} {outcome}: {note}")
        self.last_poll = 0.0

    def refresh_frame(self, ctx) -> None:
        if not self.frame_point:
            return
        fr = self.client.frame(self.frame_point, width=120)
        if fr is None:
            return
        key = (self.frame_point, len(fr.rgb), hash(fr.rgb[:64]))
        if key == self.texture_key:
            return
        self.frame = fr
        self.texture_key = key
        self.texture = _upload_texture(ctx, fr)


def _upload_texture(_ctx, fr: FrameData):
    """Hand raw RGB to the renderer as a texture. Requires the GUI stack; returns a token."""
    try:
        from imgui_bundle import immvision

        return immvision.imview  # unused sentinel; real upload happens in _draw_field
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def run_gui(url: str) -> None:
    from imgui_bundle import hello_imgui, immapp, imgui, implot, immvision
    import numpy as np

    app = App(SeamClient(url))
    tex = {"id": None, "key": None}
    app.poll()

    def gui() -> None:
        app.poll()
        st = app.state or {}
        man = app.manifest or {}
        ident = (man.get("manifest") or {}).get("identity", {})

        imgui.text(f"{(ident.get('name') or 'seam')}  ·  {ident.get('code') or ''}")
        imgui.same_line()
        imgui.text_colored((0.42, 0.42, 0.40, 1.0), f"   {man.get('mode','?')}  status={st.get('status','?')}  steps={st.get('steps',0)}")
        imgui.separator()

        if app.banner and time.time() < app.banner_until:
            imgui.text_colored((0.70, 0.28, 0.05, 1.0), app.banner)
        elif app.client.last_error:
            imgui.text_colored((0.70, 0.28, 0.05, 1.0), f"frame: {app.client.last_error}")

        avail = imgui.get_content_region_avail()
        controls_w = max(320.0, avail.x * 0.36)

        imgui.begin_child("controls", (controls_w, 0), True)
        _draw_controls(app)
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("main", (0, 0), True)
        if imgui.begin_tab_bar("tabs"):
            if imgui.begin_tab_item("Plots")[0]:
                _draw_plots(app, imgui, implot)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Field")[0]:
                _draw_field(app, imgui, immvision, np, tex)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Trace")[0]:
                _draw_trace(app, imgui)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Bench")[0]:
                _draw_bench(app, imgui)
                imgui.end_tab_item()
            imgui.end_tab_bar()
        imgui.end_child()

    params = hello_imgui.RunnerParams()
    params.callbacks.show_gui = gui
    params.app_window_params.window_title = "Seam surface (ImGui binding)"
    params.app_window_params.window_geometry.size = (1240, 780)
    params.imgui_window_params.default_imgui_window_type = \
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window
    _load_font(params)
    immapp.run(params)


def _load_font(params) -> None:
    """A CJK-capable font if the platform has one. Labels may be Chinese; boxes are ugly."""
    import os

    for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                 "/System/Library/Fonts/PingFang.ttc"):
        if os.path.exists(path):
            params.callbacks.load_additional_font(path, 16.0)
            return


def _draw_controls(app: App) -> None:
    from imgui_bundle import imgui

    controls = (app.state or {}).get("controls") or []
    interventions = [c for c in controls if c.get("role") == "intervention"]
    values = (app.state or {}).get("values") or {}

    for group, items in group_points(interventions):
        imgui.text_colored((0.42, 0.42, 0.40, 1.0), (group or "interventions").upper())
        for c in items:
            _control_row(app, c, values.get(c["name"], c.get("value")))
        imgui.spacing()


def _control_row(app: App, c: dict, value: Any) -> None:
    from imgui_bundle import imgui

    name = c["name"]
    aff = c.get("affordance")
    live = c.get("liveness")
    disabled = bool(c.get("disabled"))

    imgui.push_id(name)
    col = LIVENESS_COLOR.get(live, (0.55, 0.55, 0.55))
    imgui.text_colored(col, f"{live or '—'}")
    imgui.same_line()
    imgui.text(name)
    imgui.same_line()
    imgui.text_colored((0.6, 0.6, 0.58, 1.0), display_value(c, value))

    if aff == "dial" and c.get("domain"):
        d = c["domain"]
        lo, hi = float(d.get("lo", 0)), float(d.get("hi", 1))
        step = float(d.get("step") or (hi - lo) / 100)
        v = float(value if value is not None else lo)
        imgui.set_next_item_width(-1)
        if disabled:
            imgui.begin_disabled()
        # ImGui's slider label is a printf format; %g keeps it close to the declared precision
        # rule without reimplementing formatting inside the widget. The authoritative value is
        # shown above via display_value().
        fmt = "%.{}g".format(int(d.get("precision") or 4) or 4)
        changed, v = imgui.slider_float("##v", v, lo, hi, fmt)
        if disabled:
            imgui.end_disabled()
        if not disabled:
            # SPEC 4.7c: an imgui slider returns `changed` on every frame of a drag. Submitting
            # each one would put forty crossings in the trace for one gesture, so the value is
            # held in a draft and submitted when the widget is released.
            if changed:
                app.draft.set(name, int(v) if d.get("dtype") == "int" else v)
            if app.draft.point == name and imgui.is_item_deactivated_after_edit():
                app.commit()
    elif aff == "ratchet" and c.get("domain"):
        opts = [str(x) for x in c["domain"].get("values", [])]
        cur = str(value)
        idx = opts.index(cur) if cur in opts else 0
        imgui.set_next_item_width(-1)
        if disabled:
            imgui.begin_disabled()
        changed, idx = imgui.combo("##v", idx, opts)
        if disabled:
            imgui.end_disabled()
        if changed and not disabled:
            app.act(name, c["domain"]["values"][idx])
    elif aff == "trigger":
        armed = name in app.armed
        label = "confirm" if armed and c.get("confirm") else "fire"
        if disabled:
            imgui.begin_disabled()
        if imgui.button(label, (120, 0)) and not disabled:
            if c.get("confirm") and not armed:
                # two clicks for anything destructive; a mis-click must not kill a run
                app.armed.add(name)
                app.notify(f"{name} is destructive — click again to fire")
            else:
                app.armed.discard(name)
                app.act(name, True)
        if disabled:
            imgui.end_disabled()
    else:
        imgui.text_colored((0.6, 0.6, 0.58, 1.0), f"unresolved · {value!r}")

    if c.get("doc"):
        imgui.text_colored((0.5, 0.5, 0.48, 1.0), c["doc"][:150])
    imgui.pop_id()


def _draw_plots(app: App, imgui, implot) -> None:
    series = (app.state or {}).get("series") or {}
    if not series:
        imgui.text("no samples yet")
        return
    imgui.text_colored((0.42, 0.42, 0.40, 1.0), "per-step observations, one plot per point")
    plots = [k for k, v in series.items() if isinstance(v, list) and len(v) > 1]

    # 60 fps is the reason this binding exists: no polling inside the frame, no DOM.
    size = imgui.get_content_region_avail()
    per_plot = max(90.0, (size.y - 24.0) / max(1, min(len(plots), 4)) - 8.0)
    for name in plots[:4]:
        vals = [float(x) for x in series[name][-600:] if isinstance(x, (int, float))]
        if len(vals) < 2:
            continue
        if implot.begin_plot(name, (0, per_plot)):
            implot.setup_axes("sample", name)
            implot.plot_line(name, vals)
            implot.end_plot()


def _draw_field(app: App, imgui, immvision, np, tex) -> None:
    grid_points = [c["name"] for c in (app.state or {}).get("controls", [])
                   if c.get("role") == "observation" and c.get("shape") == "grid"]
    if not grid_points:
        imgui.text("this manifest declares no grid observation")
        return
    if imgui.begin_combo("point", app.frame_point or grid_points[0]):
        for g in grid_points:
            clicked, _ = imgui.selectable(g, g == app.frame_point)
            if clicked:
                app.frame_point = g
                tex["key"] = None
        imgui.end_combo()

    if imgui.button("fetch frame"):
        tex["key"] = None
    imgui.same_line()
    if app.frame:
        m = app.frame.meta
        imgui.text_colored(
            (0.42, 0.42, 0.40, 1.0),
            f"{app.frame.width}x{app.frame.height}  from {m.get('source_shape')}  "
            f"lo={m.get('lo', 0):.3g} hi={m.get('hi', 0):.3g}")

    fr = app.client.frame(app.frame_point, width=120)
    if fr is None:
        imgui.text_colored((0.70, 0.28, 0.05, 1.0),
                           f"no frame: {app.client.last_error or 'not produced yet'}")
        return
    key = (app.frame_point, len(fr.rgb), hash(fr.rgb[:64]))
    if tex["key"] != key:
        arr = np.frombuffer(fr.rgb, dtype=np.uint8).reshape(fr.height, fr.width, 3)
        tex["img"] = arr
        tex["key"] = key
    if "img" in tex:
        immvision.image("field", tex["img"], (0, 0))


def _draw_trace(app: App, imgui) -> None:
    records = (app.state or {}).get("records") or []
    if imgui.begin_table("trace", 5, imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_v):
        for h in ("seq", "kind", "point", "outcome", "note"):
            imgui.table_setup_column(h)
        imgui.table_headers_row()
        for r in reversed(records[-80:]):
            imgui.table_next_row()
            for k in ("seq", "kind", "point", "outcome"):
                imgui.table_next_column()
                imgui.text(str(r.get(k, "")))
            imgui.table_next_column()
            imgui.text_colored((0.5, 0.5, 0.48, 1.0), (r.get("note") or "")[:70])
        imgui.end_table()


def _draw_bench(app: App, imgui) -> None:
    man = app.manifest or {}
    controls = [c for c in (man.get("human") or []) if c.get("role") == "intervention"]
    imgui.text_colored((0.42, 0.42, 0.40, 1.0),
                       "derivation — affordance and the evidence class that settled it")
    if imgui.begin_table("bench", 4, imgui.TableFlags_.row_bg):
        for h in ("point", "affordance", "liveness", "evidence"):
            imgui.table_setup_column(h)
        imgui.table_headers_row()
        for c in controls:
            imgui.table_next_row()
            for k in (c["name"], c.get("affordance"), c.get("liveness"),
                      (c.get("provenance") or {}).get("grade")):
                imgui.table_next_column()
                imgui.text(str(k))
        imgui.end_table()

    summary = man.get("summary") or {}
    if summary.get("assumed"):
        imgui.text_colored((0.72, 0.45, 0.05, 1.0),
                           "assumed liveness: " + ", ".join(summary["assumed"]))
    if summary.get("unresolved"):
        imgui.text_colored((0.70, 0.28, 0.05, 1.0),
                           "unresolved: " + ", ".join(summary["unresolved"]))

    imgui.spacing()
    imgui.text_colored((0.42, 0.42, 0.40, 1.0), "agent projection (same manifest, other consumer)")
    tools = man.get("agent") or []
    for t in tools[:24]:
        props = ", ".join(t.get("input_schema", {}).get("properties", {}))
        imgui.text(f"{t['name']}({props})" + ("  [read-only]" if t.get("read_only") else ""))
    if man.get("diff"):
        imgui.text_colored((0.70, 0.28, 0.05, 1.0), "divergence: " + "; ".join(man["diff"]))


# ---------------------------------------------------------------------------
# headless self-test: proves the data path without opening a window
# ---------------------------------------------------------------------------


def selftest(url: str) -> int:
    c = SeamClient(url)
    fails: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(label)

    man = c.manifest()
    check("human" in man and "agent" in man, "manifest carries both projections")
    check(man.get("diff") == [], "projections agree", str(man.get("diff")))
    check(len(man.get("human", [])) > 0, "controls present", str(len(man.get("human", []))))

    st = c.state()
    check("status" in st, "state carries a status", str(list(st)[:5]))
    check(isinstance(st.get("series"), dict), "state carries series")

    grid = [x["name"] for x in st.get("controls", [])
            if x.get("role") == "observation" and x.get("shape") == "grid"]
    if grid:
        # Try every declared grid point. A surface must not dead-end because the first one
        # happens to be an on-demand artifact nobody has produced yet.
        got = None
        reasons = []
        for name in grid:
            fr = c.frame(name, width=120)
            if fr is not None:
                got = (name, fr)
                break
            reasons.append(f"{name}: {c.last_error}")
        if got is None:
            check(False, "frame for any grid point", "; ".join(reasons))
        else:
            name, fr = got
            check(len(fr.rgb) == fr.width * fr.height * 3,
                  f"frame {name} is raw RGB", f"{fr.width}x{fr.height}")
            check(fr.width <= 120, "frame respects the declared preview width", str(fr.width))
            nonblank = len(set(fr.rgb[::997]))
            check(nonblank > 3, "frame has structure, not a flat fill", str(nonblank))
    else:
        print("  ..   no grid observation in this manifest; frame channel untested")

    # a write must be answered with an outcome, never silence
    r = c.intervene("__no_such_point__", 1)
    check(r.get("outcome") == "rejected", "unknown point is rejected, not ignored", str(r))
    print()
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ImGui binding for a Seam surface")
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--selftest", action="store_true", help="exercise the data path, no window")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.url)
    try:
        import imgui_bundle  # noqa: F401
    except ImportError:
        print("imgui-bundle is not installed.  pip install imgui-bundle\n"
              "The surface itself does not need it; only this binding does.", file=sys.stderr)
        return 2
    run_gui(args.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
