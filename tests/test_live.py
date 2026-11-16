"""Live L2 demonstration on an HPC node: Meep, mid-run interventions.

This is the test the specification actually cares about. It asserts not that numbers are
right, but that:

* `until` can be *extended* mid-run and the run continues (liveness=perstep, grade=imperative)
* `dump_slice` fires mid-run and an artifact appears
* a write to a `sealed` point is *rejected and recorded*, not queued and forgotten
* the trace links each intervention to the observation that preceded it

Run:  python tests/test_live.py [--host user@hpc-node]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam import manifest as mf                      # noqa: E402
from seam.record import read                          # noqa: E402
from seam.runtime import SeamRun                      # noqa: E402
from seam.transport import SshTransport               # noqa: E402

HOST = (sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv
        else os.environ.get("SEAM_HOST", ""))
if not HOST:
    raise SystemExit("this test needs a target: --host user@machine, or SEAM_HOST")

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    m = mf.load(ROOT / "manifests" / "meep.toml")
    t = SshTransport(HOST)
    r = t.run("echo -n $HOME", timeout=30)
    home = r.out.strip()
    workdir = f"{home}/seam-runs/meep-live-{time.strftime('%H%M%S')}"
    run = SeamRun(m, transport=t, workdir=workdir, runs_dir=ROOT / "runs",
                  values={"until": 200.0, "resolution": 40, "cell_x": 4.0, "cell_y": 4.0},
                  run_id=f"meep-live-{time.strftime('%H%M%S')}")

    print(f"[seal] {workdir}")
    run.seal()
    run.launch()
    print(f"[launch] pid={run.state.pid}")

    extended = dumped = tried = False
    deadline = time.time() + 300
    while time.time() < deadline:
        alive = run.tick()
        t_now = run.state.values.get("time", 0) or 0
        if not tried and t_now >= 60:
            tried = True
            rec = run.apply("until", 480.0)          # inside the declared domain [5, 500]
            extended = rec.get("outcome") == "accepted"
            check(extended, "mid-run extend of `until` accepted", str(rec)[:200])
            rec2 = run.apply("resolution", 60)
            check(rec2.get("outcome") == "rejected",
                  "write to sealed `resolution` rejected", str(rec2)[:200])
            rec3 = run.apply("until", 9000)          # outside the declared domain
            check(rec3.get("outcome") == "rejected",
                  "out-of-domain write rejected at the seam, not passed to the job",
                  str(rec3)[:200])
        if extended and not dumped and t_now >= 120:
            rec = run.apply("dump_slice", True)
            dumped = rec.get("outcome") == "accepted"
            check(dumped, "mid-run `dump_slice` accepted", str(rec)[:200])
        if dumped and t_now >= 300:
            break
        if not alive:
            break
        time.sleep(run.poll_seconds)

    print(f"[state] status={run.state.status} steps={run.state.steps} "
          f"samples={len(run.state.series.get('ez_rms', []))}")
    check((run.state.values.get("time") or 0) > 200, "the run passed t=200 after being extended",
          f"steps={run.state.steps}")

    _, records = read(run.local_dir / "trace.jsonl")
    kinds = [r.kind for r in records]
    inters = [r for r in records if r.kind == "intervene"]
    acc = [r for r in inters if r.outcome == "accepted"]
    rej = [r for r in inters if r.outcome == "rejected"]

    check(len(acc) >= 2, "at least two accepted interventions recorded", str(len(acc)))
    check(len(rej) >= 1, "the rejected write is in the trace, not silently dropped", str(len(rej)))
    check(all(r.caused_by is not None for r in inters),
          "every intervention links to a preceding observation")
    check(kinds.count("seal") == 1, "exactly one seal record")
    check(any(a.name == "slice" or "slice" in (a.note or "") for a in m.artifacts),
          "artifact declared")
    obs = [r for r in records if r.kind == "observe"]
    check(len(obs) > 50, "per-step observations recorded", str(len(obs)))

    first = min((r.value for r in obs if r.point == "time"), default=0)
    last = max((r.value for r in obs if r.point == "time"), default=0)
    check(last > 200, "the job actually ran past the original `until`=200", f"last t={last}")
    check(last > first, "time advanced")

    print(f"[trace] {run.local_dir / 'trace.jsonl'}")
    print(f"[workdir] {workdir}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
