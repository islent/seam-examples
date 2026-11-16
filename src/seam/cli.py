"""Command line interface.

    seam check    manifests/meep.toml
    seam derive   --kind deck --path input.deck --name epoch-demo --code EPOCH3D
    seam run      manifests/meep.toml --host user@hpc-node --serve 8770
    seam scenario scenarios/meep-first-pulse.toml --specimen specimens/...
    seam specimen diff specimens/a specimens/b
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import manifest as mf
from . import projection, record, specimen as spec_mod
from .adapter import _bootstrap
from .model import Manifest, SeamError
from .runtime import SeamRun
from .transport import make

ROOT = Path(__file__).resolve().parents[2]


def _transport(args: argparse.Namespace):
    """``--host`` implies ssh; no host means local.

    The previous default pointed at one particular machine, so a fresh clone that ran
    `seam run` without flags tried to ssh to somebody else's computer.
    """
    kind = args.transport or ("ssh" if args.host else "local")
    return make(kind, args.host)


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.exists() else ROOT / path


def _workdir(transport, workdir: str | None, default: str) -> str:
    """Resolve ``$HOME`` on the *target*, so the work directory is absolute.

    An absolute path matters more than it looks: EPOCH is told its output directory on stdin,
    and a literal ``$HOME/...`` would create a directory named ``$HOME``.
    """
    w = workdir or default
    if w.startswith("$HOME"):
        r = transport.run("echo -n $HOME", timeout=30)
        home = r.out.strip()
        if home:
            w = home + w[len("$HOME"):]
    elif w.startswith("~"):
        r = transport.run("echo -n $HOME", timeout=30)
        home = r.out.strip()
        if home:
            w = home + w[1:]
    return w


def _pairs(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SeamError(f"--set expects point=value, got {it!r}")
        k, v = it.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    m = mf.load(_resolve(args.manifest))
    summary = projection.summary(m)
    print(f"manifest   {m.identity.get('name')}  ({m.identity.get('code')})")
    print(f"profile    {m.profile or '—'}   points {summary['params']} writable / "
          f"{summary['observes']} readable")
    print(f"affordance {summary['by_affordance']}")
    print(f"liveness   {summary['by_liveness']}")
    if summary["unresolved"]:
        print(f"unresolved {summary['unresolved']}")
    if summary["assumed"]:
        print(f"assumed    {summary['assumed']}  (liveness declared, not demonstrated)")
    d = projection.diff(m)
    print("projection " + ("agree" if not d else "DIVERGENT"))
    for line in d:
        print("  -", line)
    if args.agent:
        print("\nagent projection:")
        for t in projection.agent(m):
            props = ", ".join(t["input_schema"].get("properties", {}))
            mark = "  [read-only]" if t["read_only"] else ""
            print(f"  {t['name']}({props}){mark}")
    return 0 if not d else 2


def cmd_derive(args: argparse.Namespace) -> int:
    from . import probe
    from .affordance import derive_manifest, trace

    kwargs = {}
    if args.kind == "paramfile" and args.reader:
        reader = Path(args.reader).read_text(encoding="utf-8", errors="replace")
        info = probe.harvest_paramreader(reader, args.reader)
        kwargs["handlers"] = info["handlers"]
        print(f"reader grammar  {info['grammar']}   tags {len(info['tags'])}")
    if args.kind == "julia":
        kwargs["struct"] = args.struct

    raws = probe.harvest_file(args.path, args.kind, **kwargs)

    # Author-supplied bounds (gate 3 of the agent protocol). Derivation never invents them;
    # it accepts them here, at E0, and records who said so.
    bounds = {}
    for item in args.bound or []:
        name, _, span = item.partition("=")
        lo, _, hi = span.partition(",")
        bounds[name.strip()] = (float(lo), float(hi))
    for r in raws:
        if r.name in bounds:
            r.lo, r.hi = bounds[r.name]
            r.add("E0", "author", f"cli:--bound {r.name}={r.lo},{r.hi}")
    print(f"harvested {len(raws)} points from {args.path}")
    print()
    rows = trace(raws)
    w = max((len(r["name"]) for r in rows), default=8)
    print(f"{'point'.ljust(w)}  {'affordance':<11} {'liveness':<9} {'cls':<3} evidence        reason")
    for r in rows:
        reason = r["reason"] if r["affordance"] == "unresolved" else ""
        print(f"{r['name'].ljust(w)}  {r['affordance']:<11} {str(r['liveness'] or '-'):<9} "
              f"{str(r['settled_by'] or '-'):<3} {(r['evidence'] or ''):<15} {reason[:60]}")

    m = derive_manifest(
        raws, name=args.name, code=args.code, entry=args.entry,
        domain=args.domain, profile=args.profile,
        evidence_sources=[f"{args.kind}:{args.path}"],
        adapter=args.adapter,
    )
    out = Path(args.out or (ROOT / "manifests" / f"{args.name}.toml"))
    mf.save(m, out)
    print(f"\nwritten {out}")
    print("Bounds were not invented: any continuous value without a range in the source is "
          "unresolved, and needs an E0 bound with a reason (AGENT-PROTOCOL gate 3).")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    m = mf.load(_resolve(args.manifest))
    print(json.dumps(projection.agent(m), indent=2, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _bootstrap()
    m = mf.load(_resolve(args.manifest))
    overrides = _pairs(args.set)
    transport = _transport(args)
    run_id = args.run_id or f"{m.identity.get('name', 'run')}-{time.strftime('%Y%m%d-%H%M%S')}"
    workdir = _workdir(transport, args.workdir, f"$HOME/seam-runs/{run_id}")

    run = SeamRun(m, transport=transport, workdir=workdir, values=overrides,
                  run_id=run_id, decimate=args.decimate,
                  runs_dir=args.runs_dir or ROOT / "runs")
    if args.input:
        # An adapter-level input, not a manifest point: a filesystem path is not an
        # affordance, and pretending otherwise would put a text box in the surface.
        src = _workdir(transport, args.input, args.input)
        run.ctx.extra["input"] = src
    print(f"seam run   {run_id}")
    print(f"transport  {transport.name}  workdir {workdir}")
    run.seal()
    print(f"sealed     {len(run.values)} values -> {', '.join(sorted(k for k, v in run.values.items() if v is not None))[:160]}")
    run.launch()
    print(f"launched   pid {run.state.pid}")

    if args.serve:
        from .server import Surface, drive, serve

        drive(run)
        surface = Surface(m, run=run, scenario_dir=ROOT / "scenarios",
                          ui_dir=ROOT / "ui")
        try:
            serve(surface, args.serve, block=True)
        finally:
            print(f"\ntrace  {run.trace.path}")
        return 0

    run.wait(timeout=args.timeout)
    print(f"finished   status={run.state.status}  steps={run.state.steps}")
    print(f"trace      {run.trace.path}")
    if args.capture:
        dest = spec_mod.capture(str(run.local_dir), _resolve(args.capture), manifest=m)
        print(f"specimen   {dest}")
    return 0 if run.state.status == "finished" else 1


def cmd_scenario(args: argparse.Namespace) -> int:
    from .scenario import Scenario, run_live, run_specimen

    sc = Scenario.load(_resolve(args.scenario))
    if args.specimen:
        spec = spec_mod.load(_resolve(args.specimen))
        results = run_specimen(sc, spec)
    else:
        _bootstrap()
        m = mf.load(sc.manifest_path)
        transport = _transport(args)
        workdir = _workdir(transport, args.workdir,
                           f"$HOME/seam-runs/{sc.name}-{time.strftime('%H%M%S')}")
        run = SeamRun(m, transport=transport, workdir=workdir, values=_pairs(args.set),
                      run_id=sc.name, runs_dir=args.runs_dir or ROOT / "runs")
        run.seal()
        run.launch()
        results = run_live(sc, run)
        run.stop()
        print(f"\ntrace {run.trace.path}")
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed")
    return 0 if not failed else 1


def cmd_specimen(args: argparse.Namespace) -> int:
    if args.action == "open":
        spec = spec_mod.load(_resolve(args.path))
        print(f"specimen   {spec['path']}")
        print(f"identity   {spec['manifest'].identity.get('name')}")
        print(f"records    {spec['count']}")
        print(f"final      {json.dumps(spec['state'], ensure_ascii=False)[:300]}")
        print(f"writes     {len(spec['interventions'])}")
        return 0
    if args.action == "diff":
        lines = spec_mod.diff(_resolve(args.path), _resolve(args.other))
        if not lines:
            print("no differences")
            return 0
        for cat in ("contract", "shape", "outcome", "numeric"):
            group = [l for l in lines if l.category == cat]
            if not group:
                continue
            print(f"{cat}:")
            for l in group:
                print(f"  {l.point}: {l.detail}")
        return 0 if not any(l.category == "contract" for l in lines) else 2
    if args.action == "serve":
        from .server import Surface, serve

        spec = spec_mod.load(_resolve(args.path))
        surface = Surface(spec["manifest"], specimen=spec,
                          scenario_dir=ROOT / "scenarios", ui_dir=ROOT / "ui")
        serve(surface, args.port, block=True)
        return 0
    raise SeamError(f"unknown specimen action {args.action!r}")


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seam", description="Seam Specification reference tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate a manifest and diff its projections")
    c.add_argument("manifest")
    c.add_argument("--agent", action="store_true", help="also print the agent projection")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("derive", help="probe a code and emit a manifest + derivation trace")
    d.add_argument("--kind", required=True,
                   choices=["deck", "paramfile", "julia", "python", "argparse"])
    d.add_argument("--path", required=True)
    d.add_argument("--reader", help="for paramfile: the C source that parses it")
    d.add_argument("--struct", default="Simulation", help="for julia: struct name")
    d.add_argument("--name", required=True)
    d.add_argument("--code", default="unknown")
    d.add_argument("--entry", default="unknown")
    d.add_argument("--domain", default="")
    d.add_argument("--adapter", default=None)
    d.add_argument("--profile", default="L1")
    d.add_argument("--bound", action="append", metavar="POINT=LO,HI",
                   help="author-supplied bounds, recorded at E0 (derivation will not invent them)")
    d.add_argument("--out", default=None)
    d.set_defaults(func=cmd_derive)

    a = sub.add_parser("agent", help="print the agent projection as JSON")
    a.add_argument("manifest")
    a.set_defaults(func=cmd_agent)

    r = sub.add_parser("run", help="seal, launch, trace a job")
    r.add_argument("manifest")
    r.add_argument("--transport", default=None, choices=["ssh", "local"])
    r.add_argument("--host", default=os.environ.get("SEAM_HOST"))
    r.add_argument("--workdir", default=None)
    r.add_argument("--run-id", default=None)
    r.add_argument("--set", action="append", metavar="POINT=VALUE")
    r.add_argument("--timeout", type=float, default=None)
    r.add_argument("--decimate", type=int, default=1,
                   help="keep 1 in N observe records; declared in the trace")
    r.add_argument("--serve", type=int, default=None, metavar="PORT")
    r.add_argument("--capture", default=None, metavar="DIR")
    r.add_argument("--runs-dir", default=None, help="where traces are kept locally")
    r.add_argument("--input", default=None,
                   help="for read-only analyses: the work directory of a finished run")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("scenario", help="run an executable tutorial")
    s.add_argument("scenario")
    s.add_argument("--specimen", default=None, help="replay against a specimen instead of a job")
    s.add_argument("--transport", default=None, choices=["ssh", "local"])
    s.add_argument("--host", default=os.environ.get("SEAM_HOST"))
    s.add_argument("--workdir", default=None)
    s.add_argument("--set", action="append", metavar="POINT=VALUE")
    s.add_argument("--runs-dir", default=None)
    s.set_defaults(func=cmd_scenario)

    sp = sub.add_parser("specimen", help="open, diff, or serve a recorded run")
    sp.add_argument("action", choices=["open", "diff", "serve"])
    sp.add_argument("path")
    sp.add_argument("other", nargs="?")
    sp.add_argument("--port", type=int, default=8771)
    sp.set_defaults(func=cmd_specimen)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SeamError as e:
        print(f"seam: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
