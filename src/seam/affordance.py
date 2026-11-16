"""Affordance derivation — the executable form of derivation/DECISION-TABLE.md.

The point of this module is that it contains no judgement. It takes *evidence*, it emits an
*affordance plus a provenance*, and it refuses to guess. If a value cannot be settled, the
output is ``unresolved`` — which is a legal, surfaceable answer, not an error.

An agent that finds itself writing ``if "resolution" in name: affordance = "dial"`` has
left this module and is guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Domain, Manifest, Observe, Param, Provenance, SeamError

# --------------------------------------------------------------------------
# what evidence looks like
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    """One piece of evidence about one point, tagged with its ladder class."""

    grade: str          # E0..E4
    kind: str           # signature | source | argparse | deck-grammar | docstring | name | ...
    witness: str        # file:line, python:dotted.name, shell:cmd, run:<id>
    detail: Any = None


@dataclass
class RawPoint:
    """A point as harvested from a code, before any affordance is assigned."""

    name: str
    writable: bool = True
    dtype: str | None = None            # int|float|bool|str|void|list|dict|unknown
    values: list[Any] | None = None     # finite domain, if one was found
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    log: bool = False
    default: Any = None
    unit: str | None = None
    display_scale: float | None = None
    doc: str | None = None
    transition: str | None = None
    adapter_key: str | None = None
    # observations only
    shape: str | None = None
    pitch: str | None = None
    source: str | None = None
    dimensions: list[str] | None = None
    # liveness, with its own evidence
    liveness: str | None = None
    liveness_evidence: Evidence | None = None
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, grade: str, kind: str, witness: str, detail: Any = None) -> RawPoint:
        self.evidence.append(Evidence(grade=grade, kind=kind, witness=witness, detail=detail))
        return self

    @property
    def best(self) -> Evidence | None:
        for g in ("E0", "E1", "E2", "E3", "E4"):
            for e in self.evidence:
                if e.grade == g:
                    return e
        return None


@dataclass
class Decision:
    name: str
    affordance: str
    settled_by: Evidence | None
    reason: str
    liveness: str | None = None
    grade: str = "imperative"
    assumed: bool = False


# --------------------------------------------------------------------------
# the decision table
# --------------------------------------------------------------------------

_CONTINUOUS = ("int", "float")


def decide(raw: RawPoint) -> Decision:
    """writeability x domain shape x liveness -> affordance. DECISION-TABLE.md."""
    ev = raw.best

    # --- observation ------------------------------------------------------
    if not raw.writable:
        return Decision(raw.name, "gauge", ev, "read-only: the code computes it, nothing writes it")

    # --- no value ---------------------------------------------------------
    if raw.dtype == "void":
        return Decision(
            raw.name, "trigger", ev,
            "side-effecting action with no value",
            liveness=_liveness(raw, default="midrun"),
        )

    # --- finite domain ----------------------------------------------------
    if raw.dtype == "bool":
        return Decision(
            raw.name, "ratchet", ev,
            "boolean: a ratchet with two positions, not a two-stop slider",
            liveness=_liveness(raw),
        )
    if raw.values:
        return Decision(
            raw.name, "ratchet", ev,
            f"finite enumeration of {len(raw.values)} values",
            liveness=_liveness(raw),
        )

    # --- continuous -------------------------------------------------------
    if raw.dtype in _CONTINUOUS:
        if raw.lo is not None and raw.hi is not None:
            return Decision(
                raw.name, "dial", ev,
                f"continuous with declared bounds [{raw.lo}, {raw.hi}]",
                liveness=_liveness(raw),
            )
        return Decision(
            raw.name, "unresolved", ev,
            "continuous with no bounds anywhere: an unbounded dial cannot be validated, and "
            "an input that cannot be validated cannot be rejected at the schema level "
            "(SPEC 5.4, DECISION-TABLE step 2)",
        )

    # --- everything else --------------------------------------------------
    if raw.dtype == "str":
        return Decision(
            raw.name, "unresolved", ev,
            "string with no enumeration: a text box is not an affordance",
        )
    return Decision(
        raw.name, "unresolved", ev,
        f"structured value ({raw.dtype or 'unknown type'}) does not decompose into the closed set",
    )


def _liveness(raw: RawPoint, default: str = "sealed") -> str:
    """Liveness is E4, or E0-with-reason. Never E2/E3 (LADDER.md, settling rule)."""
    if raw.liveness is None:
        return default
    if raw.liveness_evidence is None:
        raise SeamError(
            f"{raw.name}: liveness={raw.liveness!r} declared with no evidence. Liveness is the "
            "field that produces decorative sliders; it needs an E4 witness or an explicit "
            "E0/assumed declaration (SPEC 4.3)."
        )
    if raw.liveness_evidence.grade not in ("E0", "E1", "E4"):
        raise SeamError(
            f"{raw.name}: liveness cannot be settled at {raw.liveness_evidence.grade} "
            f"({raw.liveness_evidence.kind}); use E4 (demonstrated) or E0 (author, with reason)"
        )
    return raw.liveness


# --------------------------------------------------------------------------
# points -> manifest entries
# --------------------------------------------------------------------------


def to_param(raw: RawPoint) -> Param:
    d = decide(raw)
    ev = d.settled_by or Evidence("E3", "name", "unknown")

    if d.affordance == "unresolved":
        return Param(
            name=raw.name,
            affordance="unresolved",
            doc=raw.doc,
            unit=raw.unit,
            provenance=Provenance(grade=ev.grade, witness=ev.witness, evidence=ev.kind,
                                  assumed=True),
        )

    if d.affordance == "trigger":
        domain = Domain(kind="void")
    elif d.affordance == "ratchet":
        vals = raw.values if raw.values is not None else [False, True]
        domain = Domain(kind="enum", values=vals)
    else:
        domain = Domain(kind="continuous", lo=raw.lo, hi=raw.hi, step=raw.step,
                        log=raw.log, dtype=raw.dtype)

    liv_ev = raw.liveness_evidence
    # A liveness claim is "assumed" unless it was demonstrated at E4 or is a structural
    # channel (E1) — SPEC 4.3 and the bench's headline warning (modes/bench.md).
    if d.liveness in ("midrun", "perstep"):
        assumed = liv_ev is None or liv_ev.grade not in ("E4", "E1")
    else:
        assumed = False

    return Param(
        name=raw.name,
        affordance=d.affordance,
        liveness=d.liveness,
        grade=d.grade,
        domain=domain,
        default=raw.default,
        unit=raw.unit,
        display_scale=raw.display_scale,
        doc=raw.doc,
        transition=raw.transition,
        adapter_key=raw.adapter_key,
        provenance=Provenance(
            grade=ev.grade,
            witness=ev.witness,
            evidence=ev.kind,
            assumed=assumed,
        ),
    )


def to_observe(raw: RawPoint) -> Observe:
    if raw.shape is None:
        raise SeamError(f"{raw.name}: an observation needs a shape (rank, not rendering)")
    if raw.pitch is None:
        raise SeamError(f"{raw.name}: an observation needs a pitch; if it cannot be told, "
                        "it is on-demand")
    ev = raw.best or Evidence("E3", "name", "unknown")
    return Observe(
        name=raw.name,
        shape=raw.shape,
        pitch=raw.pitch,
        source=raw.source or "stdout:",
        unit=raw.unit,
        dimensions=raw.dimensions,
        doc=raw.doc,
        provenance=Provenance(grade=ev.grade, witness=ev.witness, evidence=ev.kind),
    )


# --------------------------------------------------------------------------
# derivation trace
# --------------------------------------------------------------------------


def trace(raws: list[RawPoint]) -> list[dict[str, Any]]:
    """The table a reviewer checks. An implementation that cannot produce this has guessed."""
    rows = []
    for r in raws:
        d = decide(r)
        rows.append({
            "name": r.name,
            "affordance": d.affordance,
            "liveness": d.liveness,
            "settled_by": d.settled_by.grade if d.settled_by else None,
            "evidence": d.settled_by.kind if d.settled_by else None,
            "witness": d.settled_by.witness if d.settled_by else None,
            "reason": d.reason,
            "corroborating": [f"{e.grade}:{e.kind}" for e in r.evidence
                              if d.settled_by is None or e is not d.settled_by],
        })
    return rows


def derive_manifest(
    raws: list[RawPoint],
    *,
    name: str,
    code: str,
    entry: str,
    domain: str = "",
    profile: str = "L1",
    evidence_sources: list[str] | None = None,
    notes: list[dict[str, Any]] | None = None,
    cadence: dict[str, Any] | None = None,
    adapter: str | None = None,
) -> Manifest:
    identity = {"name": name, "code": code, "entry": entry}
    if domain:
        identity["domain"] = domain
    if adapter:
        identity["adapter"] = adapter
    params, observes = [], []
    for r in raws:
        if r.writable:
            params.append(to_param(r))
        else:
            observes.append(to_observe(r))
    return Manifest(
        version="0.1",
        identity=identity,
        profile=profile,
        params=params,
        observes=observes,
        notes=notes or [],
        evidence={"sources": evidence_sources or []},
        cadence=cadence or {"poll": "2s"},
    )
