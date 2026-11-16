"""Conformance tests for the Seam reference implementation.

Run with:  python -m pytest tests/ -q      (or)   python tests/test_seam.py

These test the properties the specification makes normative, not the physics:

* every manifest validates, and its two projections agree (SPEC 7.3)
* a trigger cannot be sealed, a dial cannot be unbounded, a gated write cannot be sealed
* derivation refuses to guess: unbounded continuous -> unresolved, not dial
* liveness cannot be claimed below E0/E1/E4
* the trace is append-only and links interventions to the observation that preceded them
* TOML and JSON forms round-trip losslessly and preserve order
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam import manifest as mf            # noqa: E402
from seam import projection                # noqa: E402
from seam.affordance import RawPoint, Evidence, decide, to_param, derive_manifest  # noqa: E402
from seam.model import Manifest, Param, Provenance, SeamError, Domain  # noqa: E402
from seam import probe                     # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILS.append(label)


def expect_raises(fn, exc, label: str) -> None:
    try:
        fn()
    except exc:
        print(f"  ok   {label}")
        return
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {label} (raised {type(e).__name__}: {e})")
        FAILS.append(label)
        return
    print(f"  FAIL {label} (did not raise)")
    FAILS.append(label)


# ---------------------------------------------------------------------------
print("[1] every shipped manifest validates and its projections agree")
for name in ("meep", "epoch", "gadget2", "astronbodysim"):
    m = mf.load(ROOT / "manifests" / f"{name}.toml")
    check(True, f"{name}: valid")
    d = projection.diff(m)
    check(d == [], f"{name}: projections agree" + (f" — {d}" if d else ""))

print("[2] manifest invariants are enforced, not warned")
expect_raises(
    lambda: Param(name="b", affordance="trigger", liveness="sealed", grade="imperative",
                  provenance=Provenance("E0", "test")),
    SeamError, "trigger + sealed is rejected (SPEC 4.1 inv 1)")
expect_raises(
    lambda: Domain(kind="continuous", lo=None, hi=None),
    SeamError, "continuous domain without bounds is rejected (SPEC 4.1 inv 2)")
expect_raises(
    lambda: Param(name="g", affordance="dial", liveness="sealed", grade="gated",
                  domain=Domain(kind="continuous", lo=0, hi=1),
                  provenance=Provenance("E0", "test")),
    SeamError, "gated + sealed is rejected (SPEC 4.1 inv 3)")
expect_raises(
    lambda: Param(name="x", affordance="gauge", grade="imperative",
                  provenance=Provenance("E0", "test")),
    SeamError, "gauge is not a legal intervention affordance")

print("[3] derivation refuses to guess")
unc = RawPoint(name="n_steps", writable=True, dtype="int")
unc.add("E3", "name", "deck:n_steps")
check(decide(unc).affordance == "unresolved", "unbounded continuous -> unresolved, not dial")

st = RawPoint(name="label", writable=True, dtype="str")
st.add("E3", "name", "deck:label")
check(decide(st).affordance == "unresolved", "free string -> unresolved, not a text box")

b = RawPoint(name="on", writable=True, dtype="bool")
b.add("E1", "signature", "sig:on")
check(decide(b).affordance == "ratchet", "boolean -> ratchet, not a two-stop slider")

e = RawPoint(name="bc", writable=True, dtype="str", values=["open", "periodic"])
e.add("E1", "deck-grammar", "deck:bc")
check(decide(e).affordance == "ratchet", "finite enumeration -> ratchet")

c = RawPoint(name="res", writable=True, dtype="int", lo=8, hi=200)
c.add("E1", "signature", "sig:res")
check(decide(c).affordance == "dial", "bounded continuous -> dial")

print("[4] liveness cannot be claimed without evidence")
bad = RawPoint(name="live", writable=True, dtype="float", lo=0, hi=1, liveness="midrun")
bad.add("E1", "signature", "sig:live")
bad.liveness_evidence = Evidence("E2", "docstring", "doc:live")
expect_raises(lambda: to_param(bad), SeamError,
              "midrun claimed at E2 is rejected (SPEC 4.3)")

m = mf.load(ROOT / "manifests" / "meep.toml")
t = m.param("until")
check(t.liveness == "perstep" and t.provenance.grade == "E4",
      "meep.until carries an E4 mid-run witness")

print("[5] TOML <-> JSON round-trip, order preserved")
text = mf.dumps_toml(m)
m2 = mf.loads(text, "toml")
check([p.name for p in m.params] == [p.name for p in m2.params], "param order preserved")
check([o.name for o in m.observes] == [o.name for o in m2.observes], "observe order preserved")
check(mf.dumps_json(m) == mf.dumps_json(m2), "TOML round-trip is lossless")
m3 = Manifest.from_dict(json.loads(mf.dumps_json(m)))
check(mf.dumps_json(m) == mf.dumps_json(m3), "JSON round-trip is lossless")

print("[6] a manifest carrying an unknown key is an error, not a warning")
d = m.to_dict()
d["mystery"] = {"x": 1}
expect_raises(lambda: Manifest.from_dict(d), SeamError, "unknown top-level key rejected")

print("[7] probe harvesters: E1 evidence from each exposure type")
deck = (ROOT / "manifests" / "epoch.toml").exists()  # only to keep the path honest
deck_text = """begin:control
  nx = 200
  t_end = 50 * femto
  x_max = -x_min
end:control
begin:boundaries
  bc_x_min = simple_laser
end:boundaries
"""
pts = probe.harvest_deck(deck_text, "input.deck")
names = {p.name for p in pts}
check("control.nx" in names and "control.t_end" in names, "deck keys are block-qualified")
check("boundaries.bc_x_min" in names, "deck blocks are distinguished")
tend = [p for p in pts if p.name == "control.t_end"][0]
check(tend.unit == "fs", "deck unit symbol resolved (femto -> fs)")
expr = [p for p in pts if p.name == "control.x_max"][0]
check(expr.dtype == "expression", "deck cross-reference is not treated as a literal")

pf = """InitCondFile  ic.dat
TimeMax       0.02
Nmesh         128
OutputDir     = out
"""
rpts = probe.harvest_paramfile(pf, "param.txt", handlers={"Nmesh": "int"})
byname = {p.name: p for p in rpts}
check(byname["TimeMax"].dtype == "float", "paramfile dtype from the reader")
check(byname["Nmesh"].dtype == "int", "paramfile handler overrides text appearance")
check(byname["OutputDir"].dtype == "unknown", "'Tag = Value' is flagged, not accepted")

jl = """
@kwdef struct Simulation
    TimeEnd::Float64 = 0.1
    TimeStep::Float64 = 0.0
    Realtime::Bool = false
    epsilon::Float64 = 1.0e-4
end
"""
jpts = probe.harvest_julia_struct(jl, "Config.jl", "Simulation")
jb = {p.name: p for p in jpts}
check(jb["Realtime"].dtype == "bool", "julia Bool -> bool")
check(jb["TimeEnd"].liveness == "sealed", "immutable struct field -> sealed at E1")
check(jb["epsilon"].dtype == "float", "julia Float64 -> float")

py = '''
class Simulation:
    def __init__(self, cell_size, resolution: int, Courant: float = 0.5, force_complex_fields: bool = False):
        self.resolution = resolution
        self.Courant = Courant
'''
ppts = probe.harvest_python_init(py, "meep.py", "Simulation")
pb = {p.name: p for p in ppts}
check(pb["resolution"].dtype == "int", "python annotation -> int")
check(pb["Courant"].default == 0.5, "python default harvested")
check(pb["force_complex_fields"].dtype == "bool", "python bool annotation -> bool")

print("[8] trace: append-only, causal, total")
from seam.record import Trace, read  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tr = Trace(Path(td) / "trace.jsonl", m)
    tr.seal({"resolution": 24})
    tr.transition("running")
    obs = tr.observe("ez_rms", 0.31)
    rec = tr.intervene("stop", True, "accepted", note="saw the spike")
    tr.close()
    header, records = read(Path(td) / "trace.jsonl")
    check(header.get("manifest_sha256", "").startswith("sha256:"), "trace header carries a manifest hash")
    check([r.seq for r in records] == sorted(r.seq for r in records), "sequence is monotonic")
    st = [r for r in records if r.kind == "intervene"][0]
    check(st.caused_by == obs.seq,
          "intervention links to the observation that preceded it (SPEC 6.2)")
    check(all(r.at for r in records), "every record has a timestamp")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all conformance checks passed")
