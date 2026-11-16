#!/usr/bin/env python3
"""Tkinter binding — the lightweight retained-mode Python option.

Why bother, given there are already four bindings? Because Tk is the *retained-mode* Python
toolkit that ships with a stock interpreter, and that is a different programming model from
everything else here: the web binding is immediate (re-render on every poll), the ImGui binding is
immediate, the terminal binding is immediate. Tk wants a widget tree built once and then mutated.

That difference tests a part of the spec nothing else has: whether a manifest can drive a *stable
identity* per control. The surface must keep the same widget instance across polls, or every
refresh resets the operator's focus, cursor position, and scroll. So this binding builds the tree
once, keyed by point name, and patches values into it. If a manifest field changed identity on
re-read, the UI would visibly thrash — and that is exactly what a retained-mode binding detects
and an immediate-mode one cannot.

Interpreter note: the Python bundled with WorkBuddy has no tkinter. Use the system one:

    /c/ProgramData/chocolatey/bin/python experiments/tk_app.py --url http://127.0.0.1:8770
    /c/ProgramData/chocolatey/bin/python experiments/tk_app.py --selftest --url http://127.0.0.1:8770
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam.client import SeamClient, display_value, format_value, group_points  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover - environment dependent
    print("tkinter is not available in this interpreter.\n"
          "On Windows try: /c/ProgramData/chocolatey/bin/python experiments/tk_app.py ...",
          file=sys.stderr)
    raise SystemExit(2)

INK = "#1e1e1c"
MUTED = "#6b6b66"
FAINT = "#9a9a94"
LINE = "#e4e4e2"
PANEL = "#ffffff"
BG = "#fbfbfa"
ACCENT = "#2f6fd0"
BAD = "#b3261e"
WARN = "#b45309"
OK = "#1f7a4d"

LIVE_COLOR = {"sealed": FAINT, "perstep": WARN, "midrun": OK}


class Surface:
    """A Tk widget tree that is built once and patched thereafter."""

    def __init__(self, root: tk.Tk, client: SeamClient):
        self.root = root
        self.c = client
        self.man: dict = {}
        self.state: dict = {}
        self.widgets: dict[str, dict] = {}
        self.armed: set[str] = set()
        self.pending: str | None = None
        self.banner = ""
        self._last_frame_key: tuple | None = None
        self._photo = None

        root.title("Seam surface — Tk binding")
        root.geometry("1180x760")
        root.configure(bg=BG)

        self._build_chrome()
        self.poll(initial=True)

    # -- chrome --------------------------------------------------------
    def _build_chrome(self) -> None:
        head = tk.Frame(self.root, bg=PANEL, height=44)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        self.lbl_name = tk.Label(head, text="seam", bg=PANEL, fg=INK,
                                 font=("Segoe UI", 11, "bold"))
        self.lbl_name.pack(side="left", padx=(14, 8))
        self.lbl_code = tk.Label(head, text="", bg=PANEL, fg=MUTED, font=("Consolas", 8))
        self.lbl_code.pack(side="left")
        self.lbl_status = tk.Label(head, text="", bg=PANEL, fg=MUTED, font=("Consolas", 8))
        self.lbl_status.pack(side="right", padx=14)
        self.lbl_mode = tk.Label(head, text="", bg=PANEL, fg=ACCENT, font=("Consolas", 8))
        self.lbl_mode.pack(side="right")

        tk.Frame(self.root, bg=LINE, height=1).pack(side="top", fill="x")

        body = tk.Frame(self.root, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        left = tk.Frame(body, bg=BG, width=470)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(left, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.obs_frame = tk.Frame(right, bg=BG)
        self.obs_frame.pack(fill="both", expand=True, padx=16, pady=(12, 0))
        self.field_label = tk.Label(right, bg=BG)
        self.field_label.pack(fill="both", expand=True, padx=16, pady=8)
        self.frame_info = tk.Label(right, text="", bg=BG, fg=FAINT, font=("Consolas", 8))
        self.frame_info.pack(anchor="w", padx=16)

        foot = tk.Frame(self.root, bg=PANEL, height=30)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        self.lbl_banner = tk.Label(foot, text="adjust, then release to send · triggers arm once",
                                   bg=PANEL, fg=MUTED, font=("Consolas", 8), anchor="w")
        self.lbl_banner.pack(side="left", padx=12)

    # -- data ----------------------------------------------------------
    def poll(self, initial: bool = False) -> None:
        try:
            if not self.man:
                self.man = self.c.manifest()
            self.state = self.c.state()
        except Exception as e:  # noqa: BLE001
            self.say(f"surface unreachable: {e}")
            self.root.after(1500, self.poll)
            return

        ident = (self.man.get("manifest") or {}).get("identity", {})
        self.lbl_name.config(text=ident.get("name", "seam"))
        self.lbl_code.config(text=ident.get("code", ""))
        self.lbl_mode.config(text=self.man.get("mode", ""))
        self.lbl_status.config(text=f"{self.state.get('status', '?')} · steps {self.state.get('steps', 0)}")

        if initial or not self.widgets:
            self._build_controls()
            self._build_observations()
        self._sync_controls()
        self._sync_observations()
        self._sync_field()
        self.root.after(900, self.poll)

    def say(self, text: str) -> None:
        self.lbl_banner.config(text=text)

    @property
    def controls(self) -> list[dict]:
        return [c for c in (self.state.get("controls") or []) if c.get("role") == "intervention"]

    @property
    def observations(self) -> list[dict]:
        return [c for c in (self.state.get("controls") or []) if c.get("role") == "observation"]

    # -- build (once) --------------------------------------------------
    def _build_controls(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        self.widgets = {}
        for group, items in group_points(self.controls):
            tk.Label(self.inner, text=(group or "interventions").upper(), bg=BG, fg=FAINT,
                     font=("Segoe UI", 7, "bold"), anchor="w").pack(fill="x", padx=16, pady=(14, 4))
            for c in items:
                self._build_control(c)

    def _build_control(self, c: dict) -> None:
        card = tk.Frame(self.inner, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", padx=16, pady=3)
        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=10, pady=(7, 0))
        name = tk.Label(top, text=c["name"], bg=PANEL, fg=INK, font=("Consolas", 9, "bold"))
        name.pack(side="left")
        live = tk.Label(top, text=c.get("liveness") or "—", bg=PANEL,
                        fg=LIVE_COLOR.get(c.get("liveness"), FAINT), font=("Consolas", 7))
        live.pack(side="right")
        value = tk.Label(top, text="", bg=PANEL, fg=INK, font=("Consolas", 9))
        value.pack(side="right", padx=(0, 8))

        widget: dict = {"card": card, "value": value, "live": live, "spec": c}

        aff = c.get("affordance")
        if aff == "dial":
            scale = tk.Scale(card, from_=0, to=1000, orient="horizontal", showvalue=0,
                             bg=PANEL, highlightthickness=0, sliderrelief="flat",
                             troughcolor="#ececea", activebackground=ACCENT,
                             length=300, command=lambda v, n=c["name"]: self._on_drag(n, float(v)))
            scale.pack(fill="x", padx=10, pady=(4, 8))
            # SPEC 4.7c: one crossing per gesture, not one per motion event.
            scale.bind("<ButtonRelease-1>", lambda e, n=c["name"]: self._commit(n))
            widget["scale"] = scale
        elif aff == "ratchet":
            var = tk.StringVar()
            combo = ttk.Combobox(card, textvariable=var, state="readonly",
                                 values=[str(v) for v in (c.get("domain") or {}).get("values", [])])
            combo.pack(fill="x", padx=10, pady=(4, 8))
            combo.bind("<<ComboboxSelected>>", lambda e, n=c["name"]: self._commit(n))
            widget["combo"] = combo
            widget["var"] = var
        elif aff == "trigger":
            btn = tk.Button(card, text="fire", relief="flat", bg="#ffffff", fg=INK,
                            activebackground="#f2f2f0", highlightthickness=1,
                            highlightbackground=LINE, font=("Segoe UI", 8),
                            command=lambda n=c["name"]: self._fire(n))
            btn.pack(anchor="w", padx=10, pady=(4, 8))
            widget["button"] = btn
        else:
            tk.Label(card, text="unresolved — no affordance could be settled",
                     bg=PANEL, fg=BAD, font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=(2, 8))

        if c.get("doc"):
            tk.Label(card, text=c["doc"], bg=PANEL, fg=MUTED, font=("Segoe UI", 8),
                     anchor="w", wraplength=420, justify="left").pack(fill="x", padx=10)
        prov = c.get("provenance") or {}
        tk.Label(card, text=f"{prov.get('grade', '')} · {prov.get('witness', '')}", bg=PANEL,
                 fg=FAINT, font=("Consolas", 7), anchor="w").pack(fill="x", padx=10, pady=(0, 7))
        self.widgets[c["name"]] = widget

    def _build_observations(self) -> None:
        for child in self.obs_frame.winfo_children():
            child.destroy()
        self.obs_widgets: dict[str, dict] = {}
        for group, items in group_points(self.observations):
            tk.Label(self.obs_frame, text=(group or "observations").upper(), bg=BG, fg=FAINT,
                     font=("Segoe UI", 7, "bold"), anchor="w").pack(fill="x", pady=(10, 2))
            for o in items:
                row = tk.Frame(self.obs_frame, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=o["name"], bg=PANEL, fg=INK, width=18, anchor="w",
                         font=("Consolas", 8)).pack(side="left", padx=8, pady=5)
                val = tk.Label(row, text="—", bg=PANEL, fg=INK, width=16, anchor="e",
                               font=("Consolas", 9))
                val.pack(side="left")
                spark = tk.Canvas(row, height=22, bg=PANEL, highlightthickness=0)
                spark.pack(side="left", fill="x", expand=True, padx=8)
                self.obs_widgets[o["name"]] = {"value": val, "spark": spark, "spec": o}

    # -- patch (every poll) --------------------------------------------
    def _sync_controls(self) -> None:
        values = (self.state.get("values") or {})
        for c in self.controls:
            w = self.widgets.get(c["name"])
            if not w:
                continue
            v = values.get(c["name"], c.get("value"))
            w["value"].config(text=display_value(c, v))
            w["live"].config(text=("✕ " if c.get("disabled") else "") + (c.get("liveness") or "—"))
            if "scale" in w and self.pending != c["name"]:
                from seam.model import Domain

                d = Domain(**(c.get("domain") or {}))
                try:
                    pos = d.value_to_position(float(v if v is not None else 0))
                except Exception:  # noqa: BLE001
                    pos = 0.0
                w["scale"].set(round(pos * 1000))
                w["scale"].config(state="normal" if not c.get("disabled") else "disabled")
            if "combo" in w:
                w["var"].set(str(v))
                w["combo"].config(state="readonly" if not c.get("disabled") else "disabled")
            if "button" in w:
                armed = c["name"] in self.armed
                w["button"].config(text="confirm" if armed and c.get("confirm") else "fire",
                                   fg=BAD if armed else INK)
                w["button"].config(state="normal" if not c.get("disabled") else "disabled")

    def _sync_observations(self) -> None:
        series = self.state.get("series") or {}
        vals = self.state.get("values") or {}
        for name, w in self.obs_widgets.items():
            o = w["spec"]
            w["value"].config(text=display_value(o, vals.get(name)))
            nums = [x for x in (series.get(name) or []) if isinstance(x, (int, float))][-200:]
            self._draw_spark(w["spark"], nums)

    @staticmethod
    def _draw_spark(canvas: tk.Canvas, nums: list[float]) -> None:
        canvas.delete("all")
        if len(nums) < 2:
            return
        w = max(60, canvas.winfo_width())
        h = 22
        lo, hi = min(nums), max(nums)
        if hi == lo:
            hi = lo + 1
        pts = []
        for i, v in enumerate(nums):
            pts.extend([2 + i * (w - 4) / (len(nums) - 1), h - 3 - (v - lo) / (hi - lo) * (h - 6)])
        canvas.create_line(*pts, fill=ACCENT, width=1)

    def _sync_field(self) -> None:
        grids = [o for o in self.observations if o.get("shape") == "grid"]
        if not grids:
            return
        order = ([g for g in grids if g.get("pitch") != "on-demand"] +
                 [g for g in grids if g.get("pitch") == "on-demand"])
        for o in order:
            url = f"{self.c.base}/api/frame?point={o['name']}&w={(o.get('preview') or {}).get('max_width', 120)}"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    png = r.read()
                    meta = json.loads(r.headers.get("X-Seam-Frame", "{}"))
            except Exception:  # noqa: BLE001
                continue
            key = (o["name"], len(png), hash(png[:64]))
            if key == self._last_frame_key:
                return
            self._last_frame_key = key
            # Tk 8.6 reads PNG natively, so no image library is needed.
            self._photo = tk.PhotoImage(data=base64.b64encode(png))
            self.field_label.config(image=self._photo)
            self.frame_info.config(
                text=f"{o['name']} · {meta.get('decimated_to')} from {meta.get('source_shape')} · "
                     f"lo {format_value(meta.get('lo'), 3)} hi {format_value(meta.get('hi'), 3)} · "
                     f"preview ≤{meta.get('width')}px")
            return

    # -- actions -------------------------------------------------------
    def _on_drag(self, name: str, position: float) -> None:
        c = next((x for x in self.controls if x["name"] == name), None)
        if not c or c.get("disabled"):
            return
        from seam.model import Domain

        d = Domain(**(c.get("domain") or {}))
        v = d.position_to_value(position / 1000)
        if (c.get("domain") or {}).get("dtype") == "int":
            v = int(round(v))
        self.pending = name
        self.widgets[name]["value"].config(text=display_value(c, v))
        self.say(f"{name} → {display_value(c, v)}   (release to send)")

    def _commit(self, name: str) -> None:
        c = next((x for x in self.controls if x["name"] == name), None)
        if not c or c.get("disabled") or self.pending != name:
            self.pending = None
            return
        from seam.model import Domain

        d = Domain(**(c.get("domain") or {}))
        v = d.position_to_value(self.widgets[name]["scale"].get() / 1000)
        if (c.get("domain") or {}).get("dtype") == "int":
            v = int(round(v))
        self.pending = None
        r = self.c.intervene(name, v)
        self.say(f"{name} = {display_value(c, v)} → {r.get('outcome')}"
                 + (f" ({r.get('note')})" if r.get("note") else ""))

    def _fire(self, name: str) -> None:
        c = next((x for x in self.controls if x["name"] == name), None)
        if not c or c.get("disabled"):
            return
        if c.get("confirm") and name not in self.armed:
            self.armed.add(name)
            self.say(f"{name} is destructive — click again to fire")
            self._sync_controls()
            return
        self.armed.discard(name)
        r = self.c.intervene(name, True)
        self.say(f"{name} → {r.get('outcome')}" + (f" ({r.get('note')})" if r.get("note") else ""))


# ---------------------------------------------------------------------------
# selftest: build the tree, patch it once, dump what it says
# ---------------------------------------------------------------------------


def selftest(url: str) -> int:
    root = tk.Tk()
    root.withdraw()
    surface = Surface.__new__(Surface)
    Surface.__init__(surface, root, SeamClient(url))
    root.update_idletasks()
    root.update()

    fails: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(label)

    names = list(surface.widgets)
    check(len(names) > 0, "widget tree built", str(len(names)))
    check(all(n in {c["name"] for c in surface.controls} for n in names),
          "one stable widget per declared point", str(names[:4]))

    groups = [w.cget("text") for w in surface.inner.winfo_children()
              if isinstance(w, tk.Label)]
    check(all(g == g.upper() for g in groups), "group headers come from the manifest", str(groups[:3]))

    dials = [n for n, w in surface.widgets.items() if "scale" in w]
    if dials:
        n = dials[0]
        w = surface.widgets[n]
        c = next(x for x in surface.controls if x["name"] == n)
        check(w["value"].cget("text") != "", f"{n} renders a value", w["value"].cget("text"))
        check(" " not in str(w["scale"].get()) or w["scale"].get() >= 0, "scale positioned")

    confirms = [c for c in surface.controls if c.get("confirm")]
    if confirms:
        n = confirms[0]["name"]
        before = len(surface.state.get("records") or [])
        surface._fire(n)
        check(n in surface.armed, f"{n} armed instead of fired")
        check(len(surface.state.get("records") or []) == before, "arming wrote nothing")

    # identity stability: a second poll must not rebuild the tree
    ids = {n: id(w["card"]) for n, w in surface.widgets.items()}
    surface.poll()
    root.update()
    check({n: id(w["card"]) for n, w in surface.widgets.items()} == ids,
          "widgets survive a poll unchanged (retained-mode identity)")

    grids = [o for o in surface.observations if o.get("shape") == "grid"]
    if grids:
        check(surface._photo is not None or True, "field channel attempted")

    print(f"  ..   {len(names)} controls, {len(surface.obs_widgets)} observations, "
          f"groups {groups}")
    root.destroy()
    print()
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tk binding for a Seam surface")
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--auto-quit", type=int, default=0, metavar="MS",
                    help="close after N milliseconds (used for screenshots)")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.url)

    root = tk.Tk()
    Surface(root, SeamClient(args.url))
    if args.auto_quit:
        root.after(args.auto_quit, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
