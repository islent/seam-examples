"""Specimens — frozen runs (modes/specimen.md).

A specimen is ``manifest + trace + artifacts``. It opens offline, in the same bindings, with
every control disabled. The reason it is a first-class mode rather than an export option is
``diff``: contract changes (affordance, liveness, pitch) are visible as trace diffs, and they
are invisible to a test suite that compares one final number.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import manifest as mf
from . import record as record_mod
from .model import Manifest, SeamError

FILES = ("manifest.toml", "manifest.sha256", "trace.jsonl", "state.json", "artifacts")


def capture(workdir: str, dest: str | Path, *, manifest: Manifest,
            transport=None) -> Path:
    """Freeze a finished run. Pulls the trace and the manifest; lists artifacts by name."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    trace_bytes = None
    if transport is not None:
        trace_bytes = transport.get(f"{workdir.rstrip('/')}/trace.jsonl")
    if trace_bytes is None:
        local = Path(workdir) / "trace.jsonl"
        trace_bytes = local.read_bytes() if local.exists() else None
    if trace_bytes is None:
        raise SeamError(f"no trace found in {workdir}; capture needs a recorded run")

    (dest / "trace.jsonl").write_bytes(trace_bytes)
    mf.save(manifest, dest / "manifest.toml")
    (dest / "manifest.sha256").write_text(
        record_mod.manifest_hash(manifest) + "\n", encoding="utf-8")

    state = record_mod.latest(dest / "trace.jsonl")
    (dest / "state.json").write_text(json.dumps({
        "run_id": dest.name,
        "identity": manifest.identity,
        "records": state["count"],
        "final": state["state"],
        "interventions": state["interventions"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    arts = dest / "artifacts"
    arts.mkdir(exist_ok=True)
    listing: list[dict[str, Any]] = []
    if transport is not None:
        r = transport.run(f"ls -l {workdir.rstrip('/')} 2>/dev/null | head -100", timeout=30)
        for line in r.out.splitlines():
            parts = line.split()
            if len(parts) >= 9 and not parts[0].startswith("total"):
                listing.append({"name": parts[-1], "bytes": int(parts[4]) if parts[4].isdigit() else 0})
    (arts / "index.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
    return dest


def load(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not (p / "trace.jsonl").exists():
        raise SeamError(f"{p} is not a specimen: no trace.jsonl")
    manifest = mf.load(p / "manifest.toml")
    header, records = record_mod.read(p / "trace.jsonl")
    state = record_mod.latest(p / "trace.jsonl")

    expected = None
    h = p / "manifest.sha256"
    if h.exists():
        expected = h.read_text(encoding="utf-8").strip()
    actual = record_mod.manifest_hash(manifest)
    if expected and expected != actual and header.get("manifest_sha256") != actual:
        raise SeamError(
            f"{p}: manifest.sha256 does not match manifest.toml or the trace header. "
            "A trace paired with the wrong manifest is not evidence."
        )

    return {
        "path": str(p),
        "manifest": manifest,
        "header": header,
        "records": records,
        "state": state["state"],
        "series": state["series"],
        "interventions": state["interventions"],
        "count": state["count"],
    }


@dataclass
class DiffLine:
    category: str   # contract | shape | outcome | numeric
    point: str
    detail: str


def diff(a_path: str | Path, b_path: str | Path) -> list[DiffLine]:
    """Contract diffs first, numeric drift last (modes/specimen.md)."""
    a, b = load(a_path), load(b_path)
    ma, mb = a["manifest"], b["manifest"]
    out: list[DiffLine] = []

    pa = {p.name: p for p in ma.params}
    pb = {p.name: p for p in mb.params}
    for name in sorted(set(pa) | set(pb)):
        if name not in pb:
            out.append(DiffLine("contract", name, "removed from the manifest"))
        elif name not in pa:
            out.append(DiffLine("contract", name, "added to the manifest"))
        else:
            x, y = pa[name], pb[name]
            if x.affordance != y.affordance:
                out.append(DiffLine("contract", name,
                                    f"affordance {x.affordance} -> {y.affordance} (major change)"))
            if x.liveness != y.liveness:
                out.append(DiffLine("contract", name,
                                    f"liveness {x.liveness} -> {y.liveness} (breaking: a stored "
                                    "scenario may no longer be executable, SPEC 11.4)"))
            if x.domain and y.domain and x.domain.to_dict() != y.domain.to_dict():
                out.append(DiffLine("contract", name,
                                    f"domain {x.domain.to_dict()} -> {y.domain.to_dict()}"))

    oa = {o.name: o for o in ma.observes}
    ob = {o.name: o for o in mb.observes}
    for name in sorted(set(oa) | set(ob)):
        if name not in ob:
            out.append(DiffLine("shape", name, "observation removed"))
        elif name not in oa:
            out.append(DiffLine("shape", name, "observation added"))
        else:
            if oa[name].shape != ob[name].shape:
                out.append(DiffLine("shape", name,
                                    f"shape {oa[name].shape} -> {ob[name].shape}"))
            if oa[name].pitch != ob[name].pitch:
                out.append(DiffLine("shape", name,
                                    f"pitch {oa[name].pitch} -> {ob[name].pitch}"))

    ia = {(i["point"], str(i["outcome"])) for i in a["interventions"]}
    ib = {(i["point"], str(i["outcome"])) for i in b["interventions"]}
    for point, outcome in sorted(ia - ib):
        out.append(DiffLine("outcome", point,
                            f"intervention outcome {outcome} present only in A"))
    for point, outcome in sorted(ib - ia):
        out.append(DiffLine("outcome", point,
                            f"intervention outcome {outcome} present only in B"))

    for name in sorted(set(a["series"]) & set(b["series"])):
        sa, sb = a["series"][name], b["series"][name]
        if not sa or not sb:
            continue
        tail_a, tail_b = sa[-1], sb[-1]
        if isinstance(tail_a, (int, float)) and isinstance(tail_b, (int, float)):
            denom = abs(tail_a) or 1.0
            rel = abs(tail_a - tail_b) / denom
            if rel > 1e-6:
                out.append(DiffLine("numeric", name,
                                    f"final {tail_a:.6g} vs {tail_b:.6g} ({rel:.2%})"))
    return out


def fork(spec_path: str | Path, dest: str | Path) -> Path:
    """Copy a specimen so it can be edited without destroying the evidence."""
    dest = Path(dest)
    if dest.exists():
        raise SeamError(f"{dest} already exists; specimens are immutable, fork to a new path")
    shutil.copytree(spec_path, dest)
    return dest
