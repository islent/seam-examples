"""Tk binding logic, exercised against a stub toolkit.

The machine this was developed on has no tkinter: the WorkBuddy Python is built without Tcl/Tk,
the chocolatey shim points at nothing, and there is no other interpreter. So the choice is to ship
an unverified binding or to verify the half that can be verified.

This verifies the half that matters for the specification:

* the widget tree is built **once** per point set and patched thereafter — the retained-mode
  property that no other binding here can test (an immediate-mode surface would pass regardless);
* grouping, precision and value text come from the manifest, not from the binding;
* SPEC 4.7c holds: dragging records a pending value, releasing commits exactly one crossing;
* SPEC 4.7e holds: a `confirm` trigger does not fire on the first click.

What it cannot verify: geometry, real Tk behaviour, and pixels. Those need the `--selftest` on a
machine with tkinter, which is why `experiments/tk_app.py` ships one.

    python tests/test_tk_stub.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# a stub tkinter: records the widget tree, pretends to be a toolkit
# ---------------------------------------------------------------------------


class W:
    _seq = 0

    def __init__(self, master=None, **kw):
        W._seq += 1
        self.id = W._seq
        self.master = master
        self.kw = dict(kw)
        self.children: list[W] = []
        self.bound: dict[str, object] = {}
        self._value = kw.get("value", kw.get("text", ""))
        self._destroyed = False
        if isinstance(master, W):
            master.children.append(self)

    # geometry — no-ops
    def pack(self, *a, **k): return self
    def pack_propagate(self, *a, **k): return self
    def grid(self, *a, **k): return self
    def place(self, *a, **k): return self
    def configure(self, *a, **k):
        self.kw.update(k or {})
        return self

    def config(self, *a, **k):
        self.kw.update(k or {})
        if "text" in (k or {}):
            self._value = k["text"]
        return self

    def cget(self, key):
        if key == "text":
            return self.kw.get("text", self._value)
        return self.kw.get(key, "")

    # misc
    def bind(self, event, fn=None, add=None):
        self.bound[event] = fn
        return self
    def bind_all(self, *a, **k): return self
    def winfo_children(self): return [c for c in self.children if not c._destroyed]
    def destroy(self):
        self._destroyed = True
        self.children = []
    def winfo_width(self): return 300
    def update(self): pass
    def update_idletasks(self): pass
    def after(self, ms, fn=None): return None
    def mainloop(self, *a, **k): return None
    def title(self, *a): pass
    def geometry(self, *a): pass
    def withdraw(self): pass
    def yview_scroll(self, *a): pass
    def yview(self, *a): pass
    def xview(self, *a): pass
    def yview_moveto(self, *a): pass
    def focus_set(self, *a): pass
    def see(self, *a): pass
    def create_window(self, *a, **k): return 1
    def create_line(self, *a, **k): return 1
    def delete(self, *a): pass
    def set(self, v):
        self._value = v
    def get(self):
        return self._value
    def current(self, idx): pass


class Var:
    def __init__(self, value=""): self._v = value
    def set(self, v): self._v = v
    def get(self): return self._v


class ScaleStub(W):
    def set(self, v):
        self._value = v
    def get(self):
        return self._value


def install_stub() -> None:
    tk = types.ModuleType("tkinter")
    tk.Tk = W
    tk.Frame = W
    tk.Label = W
    tk.Canvas = W
    tk.Button = W
    tk.Scale = ScaleStub
    tk.StringVar = Var
    tk.PhotoImage = W
    for name in ("BOTH", "LEFT", "RIGHT", "TOP", "BOTTOM", "X", "Y", "HORIZONTAL"):
        setattr(tk, name, name)
    ttk = types.ModuleType("tkinter.ttk")
    ttk.Scrollbar = W
    ttk.Combobox = W
    tk.ttk = ttk
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk


# ---------------------------------------------------------------------------
# a fake seam
# ---------------------------------------------------------------------------


def payload() -> tuple[dict, dict]:
    manifest = {
        "mode": "live",
        "manifest": {"identity": {"name": "stub", "code": "stub 0.1"},
                     "seam": {"version": "0.1", "profile": "L1"}},
        "human": [],
        "agent": [],
        "diff": [],
        "summary": {},
        "scenarios": [],
    }
    controls = [
        {"name": "resolution", "role": "intervention", "affordance": "dial",
         "liveness": "sealed", "grade": "imperative", "group": "grid", "precision": 0,
         "unit": "px/a", "disabled": True, "confirm": False, "value": 40,
         "doc": "grid resolution", "domain": {"kind": "continuous", "lo": 8, "hi": 200,
                                              "step": 2, "dtype": "int"},
         "provenance": {"grade": "E1", "witness": "python:test"}},
        {"name": "until", "role": "intervention", "affordance": "dial",
         "liveness": "perstep", "grade": "imperative", "group": "run control", "precision": 3,
         "unit": "a/c", "disabled": False, "confirm": False, "value": 300,
         "doc": "end time", "domain": {"kind": "continuous", "lo": 5, "hi": 500,
                                       "dtype": "float"},
         "provenance": {"grade": "E4", "witness": "run:x"}},
        {"name": "boundary", "role": "intervention", "affordance": "ratchet",
         "liveness": "sealed", "group": "grid", "precision": None, "disabled": True,
         "confirm": False, "value": "open",
         "domain": {"kind": "enum", "values": ["open", "periodic"]},
         "provenance": {"grade": "E2", "witness": "deck:x"}},
        {"name": "stop", "role": "intervention", "affordance": "trigger",
         "liveness": "midrun", "group": "run control", "precision": None, "disabled": False,
         "confirm": True, "value": None, "domain": {"kind": "void"},
         "provenance": {"grade": "E4", "witness": "run:y"}},
        {"name": "ez_rms", "role": "observation", "affordance": "gauge", "shape": "series",
         "pitch": "per-step", "group": "field", "precision": 3, "disabled": True,
         "value": 3.9999e-10, "unit": ""},
    ]
    state = {"status": "running", "steps": 12, "controls": controls,
             "values": {"resolution": 40, "until": 300, "boundary": "open", "ez_rms": 3.9999e-10},
             "series": {"ez_rms": [1e-10, 2e-10, 3.9999e-10]}, "records": []}
    manifest["human"] = controls
    return manifest, state


class FakeClient:
    base = "http://127.0.0.1:0"

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self._man, self._state = payload()

    def manifest(self): return self._man
    def state(self): return self._state

    def intervene(self, point, value):
        self.calls.append((point, value))
        return {"point": point, "outcome": "accepted"}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    install_stub()
    tk_app = load_module("tk_app", ROOT / "experiments" / "tk_app.py")

    root = tk_app.tk.Tk()
    client = FakeClient()
    surface = tk_app.Surface(root, client)

    print("[1] widget tree from the manifest")
    names = list(surface.widgets)
    # Order is group order, then manifest order within a group (SPEC 4.7a), not raw manifest order.
    check(sorted(names) == sorted(["resolution", "until", "boundary", "stop"]),
          "one widget per intervention", str(names))
    check(names.index("resolution") < names.index("until"),
          "groups are emitted in first-appearance order", str(names))
    check(surface.widgets["resolution"]["value"].cget("text") == "40 px/a",
          "precision 0 renders an exact integer with its unit",
          surface.widgets["resolution"]["value"].cget("text"))
    check(surface.widgets["until"]["value"].cget("text") == "300 a/c",
          "precision 3 renders 300 without scientific notation",
          surface.widgets["until"]["value"].cget("text"))

    print("[2] grouping comes from the manifest")
    headers = [w.cget("text") for w in surface.inner.winfo_children()
               if isinstance(w, tk_app.tk.Label)]
    check("GRID" in headers and "RUN CONTROL" in headers, "declared groups become headers", str(headers))
    check(headers.index("GRID") < headers.index("RUN CONTROL"),
          "first-appearance order preserved", str(headers))

    print("[3] retained-mode identity")
    ids = {n: id(w["card"]) for n, w in surface.widgets.items()}
    surface.poll()
    check({n: id(w["card"]) for n, w in surface.widgets.items()} == ids,
          "a poll patches the tree instead of rebuilding it")

    print("[4] commit boundary (SPEC 4.7c)")
    client.calls.clear()
    surface._on_drag("until", 750.0)
    check(surface.pending == "until", "dragging records a pending value")
    check(client.calls == [], "dragging wrote nothing", str(client.calls))
    surface.widgets["until"]["scale"].set(750)
    surface._commit("until")
    check(len(client.calls) == 1 and client.calls[0][0] == "until",
          "releasing commits exactly one crossing", str(client.calls))
    check(surface.pending is None, "pending cleared after commit")

    print("[5] arming (SPEC 4.7e)")
    client.calls.clear()
    surface._fire("stop")
    check("stop" in surface.armed, "first click arms")
    check(client.calls == [], "first click did not fire", str(client.calls))
    surface._fire("stop")
    check(len(client.calls) == 1 and client.calls[0] == ("stop", True),
          "second click fires", str(client.calls))

    print("[6] a sealed control refuses edits")
    client.calls.clear()
    surface._on_drag("resolution", 500.0)
    check(client.calls == [] and surface.pending is None,
          "dragging a disabled control does nothing")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {FAILS}")
        return 1
    print("tk binding logic ok (pixels not exercised: no tkinter in this interpreter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
