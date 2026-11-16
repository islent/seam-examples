"""Scenarios — executable tutorials (modes/scenario.md).

A scenario is an ordered list of expectations and interventions. It MUST run unchanged
against a live job and against a specimen; that requirement is what keeps a tutorial from
rotting into prose that nobody can re-run.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .model import SeamError

PREDICATES = ("lt", "le", "eq", "ne", "ge", "gt", "within_rel", "within_abs", "exists", "changes_by")


@dataclass
class Step:
    id: str
    kind: str                       # intervene | expect | assert | snapshot | note
    point: str | None = None
    op: str | None = None
    value: Any = None
    expect: str | None = None       # for intervene: accepted | deferred | rejected
    timeout: float | None = None
    tolerance: float | None = None  # for within_rel / within_abs
    within: str | None = None       # free-form window description, recorded in the trace
    text: str | None = None         # for note


@dataclass
class Scenario:
    name: str
    manifest_path: Path
    steps: list[Step]
    requires: str | None = None
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        p = Path(path)
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        head = data.get("scenario") or {}
        if "name" not in head:
            raise SeamError(f"{p}: [scenario] needs a name")
        mpath = (p.parent / head.get("manifest", "../manifests/meep.toml")).resolve()
        steps = []
        for i, s in enumerate(data.get("step", []), 1):
            kind = s.get("kind")
            if kind not in ("intervene", "expect", "assert", "snapshot", "note"):
                raise SeamError(f"{p}: step {i} has unknown kind {kind!r}")
            if kind in ("expect", "assert") and s.get("op") not in PREDICATES:
                raise SeamError(f"{p}: step {i} needs op in {PREDICATES}")
            steps.append(Step(
                id=str(s.get("id") or f"step-{i}"),
                kind=kind,
                point=s.get("point"),
                op=s.get("op"),
                value=s.get("value"),
                expect=s.get("expect"),
                timeout=_dur(s.get("timeout")),
                tolerance=s.get("tolerance"),
                within=s.get("within"),
                text=s.get("note"),
            ))
        return cls(name=head["name"], manifest_path=mpath, steps=steps,
                   requires=head.get("requires"), source_path=p)


@dataclass
class StepResult:
    step: Step
    ok: bool
    detail: str
    observed: Any = None


def _dur(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip()
    if t.endswith("ms"):
        return float(t[:-2]) / 1000
    if t.endswith("s"):
        return float(t[:-1])
    if t.endswith("m"):
        return float(t[:-1]) * 60
    return float(t)


def evaluate(op: str, observed: Any, target: Any, tolerance: float | None = None) -> bool:
    if op == "exists":
        return observed is not None
    if observed is None:
        return False
    if op == "lt":
        return observed < target
    if op == "le":
        return observed <= target
    if op == "eq":
        return observed == target
    if op == "ne":
        return observed != target
    if op == "ge":
        return observed >= target
    if op == "gt":
        return observed > target
    if op == "within_rel":
        return abs(observed - target) <= abs(target) * float(tolerance or 0.05)
    if op == "within_abs":
        return abs(observed - target) <= float(tolerance or 0.0)
    if op == "changes_by":
        return abs(observed) >= abs(target)
    raise SeamError(f"unknown predicate {op!r}")


# ---------------------------------------------------------------------------
# live execution
# ---------------------------------------------------------------------------


def run_live(sc: Scenario, run, *, out: Callable[[str], None] = print,
             on_step: Callable[[StepResult], None] | None = None) -> list[StepResult]:
    results: list[StepResult] = []
    for step in sc.steps:
        res = _live_step(step, run, out)
        results.append(res)
        if on_step:
            on_step(res)
        if not res.ok:
            out(f"  step {step.id} failed: {res.detail}")
            break
    return results


def _live_step(step: Step, run, out: Callable[[str], None]) -> StepResult:
    out(f"[{step.id}] {step.kind} {step.point or ''}".rstrip())

    if step.kind == "note":
        out(f"  {step.text}")
        return StepResult(step, True, "note")

    if step.kind == "intervene":
        outcome = run.apply(step.point, step.value, actor="scenario")
        got = outcome.get("outcome")
        want = step.expect or "accepted"
        ok = got == want
        detail = f"{step.point}={step.value!r} -> {got}" + (f" ({outcome.get('note')})"
                                                            if outcome.get("note") else "")
        out(f"  {detail}")
        return StepResult(step, ok, detail, observed=got)

    if step.kind == "snapshot":
        return StepResult(step, True, "snapshot", observed=run.state.to_dict())

    deadline = time.time() + (step.timeout or 120)
    while time.time() < deadline:
        observed = run.state.values.get(step.point)
        if step.kind == "assert":
            ok = evaluate(step.op, observed, step.value, step.tolerance)
            out(f"  assert {step.point} {step.op} {step.value}: got {observed} -> {'ok' if ok else 'FAIL'}")
            return StepResult(step, ok, f"assert {step.point} {step.op} {step.value}", observed)
        ok = evaluate(step.op, observed, step.value, step.tolerance)
        if ok:
            out(f"  {step.point} {step.op} {step.value} reached (observed {observed})")
            return StepResult(step, True, f"expect {step.point} {step.op} {step.value}", observed)
        if not run.tick():
            out("  job ended before the expectation was met")
            return StepResult(step, False, "job ended", observed)
        time.sleep(run.poll_seconds)
    out(f"  timed out waiting for {step.point} {step.op} {step.value}")
    return StepResult(step, False, "timeout", run.state.values.get(step.point))


# ---------------------------------------------------------------------------
# specimen execution (offline)
# ---------------------------------------------------------------------------


def run_specimen(sc: Scenario, specimen, *, out: Callable[[str], None] = print,
                 on_step: Callable[[StepResult], None] | None = None,
                 fast: bool = True) -> list[StepResult]:
    """Replay a scenario against a recorded trace. No job, no network (modes/specimen.md)."""
    series = specimen["series"]
    results: list[StepResult] = []
    cursor: dict[str, int] = {k: 0 for k in series}

    for step in sc.steps:
        if step.kind == "intervene":
            results.append(StepResult(
                step, False,
                "specimens accept no writes; fork this scenario into a live run to explore",
                observed=None))
            out(f"[{step.id}] intervene {step.point} — refused (specimen is read-only)")
            break
        if step.kind == "note":
            out(f"[{step.id}] {step.text}")
            results.append(StepResult(step, True, "note"))
            continue
        if step.kind == "snapshot":
            results.append(StepResult(step, True, "snapshot"))
            continue

        vals = series.get(step.point or "", [])
        ok = False
        observed = None
        for v in vals:
            observed = v
            if evaluate(step.op, v, step.value, step.tolerance):
                ok = True
                break
        detail = f"{step.point} {step.op} {step.value} over {len(vals)} recorded samples"
        out(f"[{step.id}] {detail} -> {'ok' if ok else 'FAIL'}")
        results.append(StepResult(step, ok, detail, observed))

    if on_step:
        for r in results:
            on_step(r)
    return results
