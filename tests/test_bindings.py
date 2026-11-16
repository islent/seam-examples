"""Two surfaces, one manifest — the claim that `Binding` is replaceable, tested.

This starts a real Meep run on the target, serves it, and then exercises what the repository
ships:

    1. the web surface      (SSE + PNG frames, as a browser sees it)
    2. the terminal surface (HTTP poll, layout/grouping/draft/arming via --selftest)

Two bindings from `experiments/` are checked as well when their dependencies are present — the
ImGui data path and the Electron data path — and skipped with a note when they are not.

Every consumer reads the same manifest and may differ only in how things look. If they disagree
about what may be written, the implementation is non-conformant (SPEC §7.3).

Run:  python tests/test_bindings.py [--host user@hpc-node]
"""

from __future__ import annotations

import json
import subprocess
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam import manifest as mf                     # noqa: E402
from seam.manifest import load                      # noqa: E402
from seam.runtime import SeamRun                    # noqa: E402
from seam.server import Surface, drive, serve       # noqa: E402
from seam.transport import SshTransport             # noqa: E402

HOST = (sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv
        else os.environ.get("SEAM_HOST", ""))
if not HOST:
    raise SystemExit("this test needs a target: --host user@machine, or SEAM_HOST")

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
FAILS: list[str] = []


def check(cond, label, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def sse_events(seconds: float = 3.0) -> int:
    """Count `event: state` frames from the event stream, as a browser would."""
    count = 0
    req = urllib.request.Request(BASE + "/api/events")
    try:
        with urllib.request.urlopen(req, timeout=seconds + 5) as r:
            deadline = time.time() + seconds
            buf = ""
            while time.time() < deadline:
                chunk = r.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", "replace").strip()
                if line.startswith("event: state"):
                    count += 1
    except Exception:  # noqa: BLE001 - a closed stream is not a test failure by itself
        pass
    return count


def main() -> int:
    m = load(ROOT / "manifests" / "meep.toml")
    t = SshTransport(HOST)
    home = t.run("echo -n $HOME").out.strip()
    run = SeamRun(m, transport=t, workdir=f"{home}/seam-runs/bindings",
                  runs_dir=ROOT / "runs", run_id=f"bindings-{time.strftime('%H%M%S')}",
                  values={"until": 600.0, "resolution": 40, "cell_x": 4.0, "cell_y": 4.0})

    print(f"[run] {run.run_id}")
    run.seal()
    run.launch()
    drive(run)

    surface = Surface(m, run=run, scenario_dir=ROOT / "scenarios", ui_dir=ROOT / "ui")
    httpd = serve(surface, PORT, block=False)
    time.sleep(2)

    # Turn on the declared frame capability; this is an ordinary intervention, traced like any.
    rec = run.apply("stream_frames", True)
    check(rec.get("outcome") == "accepted", "stream_frames accepted", str(rec)[:160])

    # Wait for the producer to actually write a preview before asking for one.
    ready = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{BASE}/api/frame?point=ez_frame&w=120", timeout=10) as r:
                r.read()
                ready = True
                break
        except urllib.error.HTTPError:
            time.sleep(0.5)
    check(ready, "a preview crossed the seam")

    print("\n[1] web surface")
    with urllib.request.urlopen(BASE + "/api/manifest") as r:
        man = json.loads(r.read())
    check(man["diff"] == [], "web: projections agree", str(man["diff"]))
    with urllib.request.urlopen(f"{BASE}/api/frame?point=ez_frame&w=120") as r:
        png = r.read()
        meta = json.loads(r.headers.get("X-Seam-Frame", "{}"))
    check(png[:8] == b"\x89PNG\r\n\x1a\n", "web: frame is a PNG")
    check(len(png) < 60_000, "web: frame is a few tens of KB, not the array", f"{len(png)}B")
    check(meta.get("source_shape") == [80, 80], "web: preview reports its source shape", str(meta.get("source_shape")))
    events = sse_events(3.0)
    check(events >= 1, "web: event stream pushes state frames", str(events))

    print("\n[2] ImGui binding (experiments/, skipped if absent)")
    r = subprocess.run([sys.executable, str(ROOT / "experiments" / "implot_app.py"),
                        "--selftest", "--url", BASE],
                       capture_output=True, text=True, timeout=180)
    print(r.stdout.rstrip() or r.stderr.rstrip())
    check(r.returncode == 0, "imgui: data path selftest", r.stderr[-200:] if r.returncode else "")

    print("\n[3] Electron host (experiments/)")
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        print("  ..   node not available; skipping the Electron data path")
    else:
        r = subprocess.run(["node", str(ROOT / "experiments" / "electron" / "selftest.js"), "--url", BASE],
                           capture_output=True, text=True, timeout=180)
        print(r.stdout.rstrip() or r.stderr.rstrip())
        check(r.returncode == 0, "electron: data path selftest", r.stderr[-200:] if r.returncode else "")

    print("\n[4] terminal binding")
    r = subprocess.run([sys.executable, str(ROOT / "tui" / "seam_tui.py"),
                        "--selftest", "--url", BASE, "--width", "100", "--height", "34"],
                       capture_output=True, text=True, timeout=180)
    tail = [ln for ln in r.stdout.splitlines() if ln.strip().startswith(("ok", "FAIL"))]
    print("\n".join(tail[-6:]) or r.stderr[-300:])
    check(r.returncode == 0, "tui: layout, grouping, draft and arming", r.stdout[-200:] if r.returncode else "")

    print("\n[5] format parity")
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "test_format_parity.py")],
                       capture_output=True, text=True, timeout=120)
    print("\n".join([ln for ln in r.stdout.splitlines() if ln.strip()][-4:]))
    check(r.returncode == 0, "format: python, javascript and julia agree digit for digit")

    print("\n[6] admission is binding-independent")
    # The same write through two different clients must get the same verdict.
    a = run.apply("resolution", 60)
    with urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/intervene", data=json.dumps({"point": "resolution", "value": 60}).encode(),
            headers={"Content-Type": "application/json"})) as resp:
        b = json.loads(resp.read())
    check(a["outcome"] == b["outcome"] == "rejected",
          "both clients get the same verdict for a sealed write", f"{a['outcome']} vs {b['outcome']}")

    surface.closed = True
    httpd.shutdown()
    print(f"\n[trace] {run.local_dir / 'trace.jsonl'}")
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {FAILS}")
        return 1
    print("all binding checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
