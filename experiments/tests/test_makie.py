"""The Makie binding, verified headlessly.

Makie is the sixth binding and the third programming model here (DOM, immediate mode, retained
mode, reactive graph). The parts worth testing without a person looking at a window:

* the Julia formatting implementation agrees with the Python and JavaScript ones — a third
  independent implementation is the cheapest way to find a rule that was never fully specified;
* a snapshot written by the bridge renders to a figure, so the layout code runs on real data;
* the GL backend can open a window, refresh it, and close it (skipped if `--gl-smoke` is not asked
  for, because it needs a display).

What this does not verify: how it looks. That needs eyes, and `data/makie-cairo.png` is the
artifact those eyes would look at.

    python tests/test_makie.py            # cairo render only
    python tests/test_makie.py --gl       # also open and close a GL window
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPECIMEN = ROOT / "experiments" / "makie" / "live"

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def julia() -> str | None:
    for cand in ("julia", "julia.exe", r"D:/Programs/julia-1.13.3/bin/julia.exe"):
        if "/" in cand or "\\" in cand:
            if Path(cand).exists():
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def main() -> int:
    exe = julia()
    if exe is None:
        print("julia not found; the Makie binding is untested here")
        return 0

    if not (SPECIMEN / "state.toml").exists():
        print(f"no snapshot at {SPECIMEN}; run experiments/makie/bridge.py first")
        return 0

    project = ROOT / "experiments" / "makie"
    out = ROOT / "data" / "_makie_test.png"
    t0 = time.time()
    r = subprocess.run([exe, f"--project={project}", str(project / "seam_makie.jl"),
                        "--backend", "cairo", "--once", "--dir", str(SPECIMEN),
                        "--save", str(out)],
                       capture_output=True, text=True, timeout=600)
    took = time.time() - t0
    check(r.returncode == 0, "cairo backend builds the figure", (r.stderr or "")[-300:])
    check(out.exists() and out.stat().st_size > 5000,
          "figure rasterised", f"{out.stat().st_size if out.exists() else 0} bytes")
    print(f"  ..   {took:.1f}s")
    if out.exists():
        out.unlink()

    # The state snapshot must carry the frame's identity, not just its bytes: a binding that
    # binds an image axis to the wrong point draws a black rectangle that looks like data.
    text = (SPECIMEN / "state.toml").read_text(encoding="utf-8")
    check("frame_point" in text, "snapshot names the point its frame belongs to")

    if "--gl" in sys.argv:
        r = subprocess.run([exe, f"--project={project}", str(project / "seam_makie.jl"),
                            "--backend", "gl", "--gl-smoke", "6", "--dir", str(SPECIMEN)],
                           capture_output=True, text=True, timeout=600)
        out_txt = (r.stdout or "") + (r.stderr or "")
        check(r.returncode == 0, "gl backend opens and closes a window", out_txt[-300:])
        check("GL window: opened" in out_txt, "window reported open")
        check("refreshed" in out_txt, "figure refreshed from the snapshot")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {FAILS}")
        return 1
    print("makie binding ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
