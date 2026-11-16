"""Screenshot the web surface with a live run behind it. Dev helper, not a test.

    python tests/shot_ui.py --tab field --out data/shot-field.png

Requires `npx playwright` (Chromium). Kept in the repository because a surface nobody has
looked at is a surface nobody has verified.
"""

from __future__ import annotations

import argparse
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam.manifest import load              # noqa: E402
from seam.runtime import SeamRun            # noqa: E402
from seam.server import Surface, drive, serve  # noqa: E402
from seam.transport import SshTransport     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", default="run")
    ap.add_argument("--out", default="data/shot.png")
    ap.add_argument("--host", default=os.environ.get("SEAM_HOST"))
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--manifest", default="manifests/meep.toml")
    ap.add_argument("--wait", type=float, default=4.0)
    args = ap.parse_args()

    m = load(ROOT / args.manifest)
    t = SshTransport(args.host)
    home = t.run("echo -n $HOME").out.strip()
    run = SeamRun(m, transport=t, workdir=f"{home}/seam-runs/shot",
                  runs_dir=ROOT / "runs", run_id=f"shot-{time.strftime('%H%M%S')}",
                  values={"until": 900.0, "resolution": 40, "cell_x": 4.0, "cell_y": 4.0})
    run.seal()
    run.launch()
    drive(run)
    surface = Surface(m, run=run, scenario_dir=ROOT / "scenarios", ui_dir=ROOT / "ui")
    httpd = serve(surface, args.port, block=False)
    time.sleep(2)
    run.apply("stream_frames", True)
    time.sleep(args.wait)

    url = f"http://127.0.0.1:{args.port}/#{args.tab}"
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    npx = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    r = subprocess.run([npx, "--yes", "playwright", "screenshot",
                        "--viewport-size=1280,820", "--wait-for-timeout=3500", url, str(out)],
                       capture_output=True, text=True, timeout=300)
    print(r.stdout.strip() or r.stderr.strip()[:400])
    surface.closed = True
    httpd.shutdown()
    print("written", out)
    return 0 if out.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
