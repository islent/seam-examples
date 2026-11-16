"""Seam data model.

Plain dataclasses, no dependencies. Everything here mirrors
``seam/schema/seam-manifest.schema.json`` one-for-one, so that the Python
validator and the JSON Schema cannot drift apart without one of them failing.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

VERSION = "0.1"

AFFORDANCES = ("trigger", "dial", "ratchet", "gauge", "unresolved")
LIVENESS = ("sealed", "midrun", "perstep")
GRADES = ("advisory", "imperative", "gated")
SHAPES = ("scalar", "series", "grid", "table", "log")
ARTIFACT_SHAPES = ("grid", "table", "series", "blob")
PROFILES = ("L0", "L1", "L2")
EVIDENCE_CLASSES = ("E0", "E1", "E2", "E3", "E4")


class SeamError(Exception):
    """A manifest or protocol violation. Never raised for runtime conditions."""


@dataclass
class Provenance:
    grade: str
    witness: str
    evidence: str | None = None
    assumed: bool = False

    def __post_init__(self) -> None:
        if self.grade not in EVIDENCE_CLASSES:
            raise SeamError(f"provenance grade must be one of {EVIDENCE_CLASSES}, got {self.grade!r}")
        if not self.witness:
            raise SeamError("provenance.witness is required: a locator a third party can check")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"grade": self.grade, "witness": self.witness}
        if self.evidence:
            d["evidence"] = self.evidence
        if self.assumed:
            d["assumed"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Provenance:
        return cls(
            grade=d["grade"],
            witness=d["witness"],
            evidence=d.get("evidence"),
            assumed=bool(d.get("assumed", False)),
        )


@dataclass
class Domain:
    kind: str  # continuous | enum | void
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    log: bool = False
    values: list[Any] | None = None
    dtype: str | None = None  # int | float | bool | str

    def __post_init__(self) -> None:
        if self.kind not in ("continuous", "enum", "void"):
            raise SeamError(f"domain.kind must be continuous|enum|void, got {self.kind!r}")
        if self.log and (self.lo is None or self.hi is None):
            raise SeamError("log requires finite lo and hi")
        if self.log and self.lo is not None and self.lo <= 0:
            raise SeamError("a log domain needs lo > 0")
        if self.kind == "continuous":
            if self.lo is None or self.hi is None:
                raise SeamError(
                    "a continuous domain needs finite lo and hi; an unbounded dial cannot be "
                    "validated, and cannot be rejected at the schema level (SPEC 4.1)"
                )
            if self.lo >= self.hi:
                raise SeamError(f"domain lo ({self.lo}) must be < hi ({self.hi})")
        if self.kind == "enum" and not self.values:
            raise SeamError("an enum domain needs a non-empty values list")

    def position_to_value(self, t: float) -> float:
        """SPEC 4.7. How a control position maps to a value.

        Linear by default; geometric when ``log`` is set, so that a control's *travel* is
        uniform in the exponent. Without a normative mapping, two bindings give a slider two
        different meanings — the same defect as two different previews.
        """
        t = min(1.0, max(0.0, t))
        lo, hi = float(self.lo), float(self.hi)
        if self.log:
            if lo <= 0:
                raise SeamError("a log domain needs lo > 0")
            import math

            return lo * (hi / lo) ** t
        return lo + (hi - lo) * t

    def value_to_position(self, v: float) -> float:
        import math

        lo, hi = float(self.lo), float(self.hi)
        v = min(hi, max(lo, float(v)))
        if self.log:
            if lo <= 0:
                raise SeamError("a log domain needs lo > 0")
            return math.log(v / lo) / math.log(hi / lo)
        return (v - lo) / (hi - lo) if hi != lo else 0.0

    def admits(self, value: Any) -> bool:
        if self.kind == "void":
            return True
        if self.kind == "enum":
            return value in (self.values or [])
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        return self.lo <= v <= self.hi

    def clamp(self, value: Any) -> Any:
        """Clamp into the declared domain. Used by renderers, never by the runtime."""
        if self.kind != "continuous":
            return value
        v = float(value)
        return min(max(v, self.lo), self.hi)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        # Note: `0 not in (None, False, [])` is True in Python (0 == False), so a membership
        # test would silently drop lo=0 / step=0 — which is how a dial loses its floor.
        for k in ("lo", "hi", "step", "dtype"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.log:
            d["log"] = True
        if self.values:
            d["values"] = list(self.values)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Domain | None:
        if d is None:
            return None
        return cls(**d)


@dataclass
class Param:
    """An Intervention Point. SPEC 4.1."""

    name: str
    affordance: str
    provenance: Provenance
    liveness: str | None = None
    grade: str | None = None
    domain: Domain | None = None
    default: Any = None
    unit: str | None = None
    display_scale: float | None = None
    doc: str | None = None
    transition: str | None = None
    timeout: str | None = None
    adapter_key: str | None = None
    #: SPEC 4.7. Layout section. Bindings MUST NOT invent one: grouping is presentation
    #: order, and presentation order is part of the manifest.
    group: str | None = None
    #: SPEC 4.7. Significant digits for display. Two bindings that format one value two
    #: different ways are showing two different numbers.
    precision: int | None = None
    #: SPEC 4.7. A destructive trigger must be armed before it fires.
    confirm: bool = False

    def __post_init__(self) -> None:
        if self.precision is not None and not (0 <= self.precision <= 12):
            raise SeamError(f"{self.name}: precision must be 0..12 (0 = exact integer)")
        if self.confirm and self.affordance != "trigger":
            raise SeamError(f"{self.name}: confirm only applies to triggers")
        if self.affordance not in AFFORDANCES:
            raise SeamError(f"{self.name}: affordance must be one of {AFFORDANCES}")
        if self.affordance == "gauge":
            raise SeamError(f"{self.name}: gauge is not a legal affordance for an intervention point")
        if self.affordance == "unresolved":
            return
        if self.liveness not in LIVENESS:
            raise SeamError(f"{self.name}: liveness must be one of {LIVENESS}")
        if self.grade not in GRADES:
            raise SeamError(f"{self.name}: grade must be one of {GRADES}")
        # SPEC 4.1 invariant 1
        if self.affordance == "trigger" and self.liveness == "sealed":
            raise SeamError(
                f"{self.name}: trigger with liveness=sealed has no meaning; a value settable "
                "only before submission is a dial or a ratchet"
            )
        # SPEC 4.1 invariant 3
        if self.grade == "gated" and self.liveness == "sealed":
            raise SeamError(f"{self.name}: grade=gated needs something running to gate")
        # SPEC 4.1 invariant 2
        if self.affordance == "dial":
            if self.domain is None or self.domain.kind != "continuous":
                raise SeamError(f"{self.name}: a dial needs a continuous domain with finite lo/hi")
        if self.affordance == "ratchet":
            if self.domain is None or self.domain.kind != "enum":
                raise SeamError(f"{self.name}: a ratchet needs an enum domain")
        if self.affordance == "trigger":
            if self.domain is None:
                self.domain = Domain(kind="void")
            if self.domain.kind != "void":
                raise SeamError(f"{self.name}: a trigger carries no value; domain must be void")

    @property
    def live(self) -> bool:
        return self.liveness in ("midrun", "perstep")

    def encode(self, value: Any) -> Any:
        """Convert a display value to the value written to the code."""
        if self.display_scale is not None and isinstance(value, (int, float)):
            return float(value) / self.display_scale
        return value

    def decode(self, value: Any) -> Any:
        if self.display_scale is not None and isinstance(value, (int, float)):
            return float(value) * self.display_scale
        return value

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "affordance": self.affordance,
            "provenance": self.provenance.to_dict(),
        }
        for k in ("liveness", "grade", "default", "unit", "display_scale", "doc",
                  "transition", "timeout", "adapter_key", "group", "precision"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.confirm:
            d["confirm"] = True
        if self.domain is not None:
            d["domain"] = self.domain.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Param:
        d = dict(d)
        d["provenance"] = Provenance.from_dict(d["provenance"])
        d["domain"] = Domain.from_dict(d.get("domain"))
        return cls(**d)


@dataclass
class Observe:
    """An Observation Point. SPEC 4.2."""

    name: str
    shape: str
    pitch: str
    source: str
    provenance: Provenance
    unit: str | None = None
    dimensions: list[str] | None = None
    doc: str | None = None
    group: str | None = None
    precision: int | None = None
    #: SPEC 4.6. Required for a grid at a live pitch: what crosses the seam is a preview,
    #: never the array. ``{"max_width": 480, "diverging": True}``
    preview: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.shape not in SHAPES:
            raise SeamError(f"{self.name}: shape must be one of {SHAPES}")
        if not (self.pitch in ("per-step", "on-demand") or self.pitch.startswith("per-second:")):
            raise SeamError(f"{self.name}: pitch must be per-step, on-demand, or per-second:<n>")
        if ":" not in self.source:
            raise SeamError(f"{self.name}: source must be stdout:…, file:…, api:…, or metric:…")
        kind = self.source.split(":", 1)[0]
        if kind not in ("stdout", "file", "api", "metric"):
            raise SeamError(f"{self.name}: unknown source kind {kind!r}")
        # SPEC 4.6: a grid shipped whole at a live pitch is not a seam, it is a bandwidth bill.
        if self.shape == "grid" and self.pitch != "on-demand" and not self.preview:
            raise SeamError(
                f"{self.name}: a grid at pitch={self.pitch!r} must declare `preview` "
                "(max_width / diverging), or be declared on-demand. What crosses the seam is "
                "a reduced frame, never the array (SPEC 4.6)."
            )

    @property
    def per_step(self) -> bool:
        return self.pitch == "per-step"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "shape": self.shape,
            "pitch": self.pitch,
            "source": self.source,
            "provenance": self.provenance.to_dict(),
        }
        for k in ("unit", "dimensions", "doc", "preview", "group"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.precision is not None:
            d["precision"] = self.precision
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observe:
        d = dict(d)
        d["provenance"] = Provenance.from_dict(d["provenance"])
        return cls(**d)


@dataclass
class Artifact:
    """A file-backed product. SPEC 4.5."""

    name: str
    path: str
    shape: str
    pitch: str
    provenance: Provenance
    doc: str | None = None

    def __post_init__(self) -> None:
        if self.shape not in ARTIFACT_SHAPES:
            raise SeamError(f"{self.name}: artifact shape must be one of {ARTIFACT_SHAPES}")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "path": self.path,
            "shape": self.shape,
            "pitch": self.pitch,
            "provenance": self.provenance.to_dict(),
        }
        if self.doc:
            d["doc"] = self.doc
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Artifact:
        d = dict(d)
        d["provenance"] = Provenance.from_dict(d["provenance"])
        return cls(**d)


@dataclass
class Manifest:
    version: str
    identity: dict[str, Any]
    params: list[Param] = field(default_factory=list)
    observes: list[Observe] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    cadence: dict[str, Any] = field(default_factory=dict)
    profile: str | None = None
    source_path: str | None = None

    # -- lookups ----------------------------------------------------------
    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise SeamError(f"no intervention point named {name!r}")

    def observe(self, name: str) -> Observe:
        for o in self.observes:
            if o.name == name:
                return o
        raise SeamError(f"no observation point named {name!r}")

    def has(self, name: str) -> bool:
        return any(p.name == name for p in self.params) or any(o.name == name for o in self.observes)

    @property
    def adapter_id(self) -> str:
        return str(self.identity.get("adapter") or self.identity.get("code", "").split()[0].lower())

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seam": {"version": self.version},
            "identity": self.identity,
        }
        if self.profile:
            d["seam"]["profile"] = self.profile
        if self.evidence:
            d["evidence"] = self.evidence
        if self.cadence:
            d["cadence"] = self.cadence
        if self.params:
            d["param"] = [p.to_dict() for p in self.params]
        if self.observes:
            d["observe"] = [o.to_dict() for o in self.observes]
        if self.artifacts:
            d["artifact"] = [a.to_dict() for a in self.artifacts]
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], source_path: str | None = None) -> Manifest:
        seam = d.get("seam") or {}
        version = seam.get("version")
        if not version:
            raise SeamError("manifest is missing seam.version")
        unknown = set(d) - {"seam", "identity", "evidence", "cadence", "param",
                            "observe", "artifact", "notes"}
        if unknown:
            # SPEC 3.2: unknown keys are an error, not a warning.
            raise SeamError(
                f"unknown manifest keys: {sorted(unknown)}. A manifest carrying a key nobody "
                "reads is a manifest whose author believed something false."
            )
        m = cls(
            version=version,
            identity=d.get("identity") or {},
            profile=seam.get("profile"),
            evidence=d.get("evidence") or {},
            cadence=d.get("cadence") or {},
            params=[Param.from_dict(x) for x in d.get("param", [])],
            observes=[Observe.from_dict(x) for x in d.get("observe", [])],
            artifacts=[Artifact.from_dict(x) for x in d.get("artifact", [])],
            notes=d.get("notes") or [],
            source_path=source_path,
        )
        m.validate()
        return m

    def validate(self) -> None:
        names: set[str] = set()
        for p in self.params:
            if p.name in names:
                raise SeamError(f"duplicate point name {p.name!r}")
            names.add(p.name)
        for o in self.observes:
            if o.name in names:
                raise SeamError(f"duplicate point name {o.name!r}")
            names.add(o.name)
        for a in self.artifacts:
            if a.name in names:
                raise SeamError(f"duplicate point name {a.name!r}")
            names.add(a.name)
        for p in self.params:
            if p.affordance == "unresolved" or p.domain is None or p.domain.kind == "void":
                continue
            if p.default is None:
                continue
            if not p.domain.admits(p.default):
                raise SeamError(
                    f"{p.name}: default {p.default!r} is outside its declared domain "
                    f"{p.domain.to_dict()}. A manifest whose default contradicts its own "
                    "statement about admissibility is a defect (SPEC 4.1 invariant 5)."
                )
        if self.profile and self.profile not in PROFILES:
            raise SeamError(f"profile must be one of {PROFILES}")
        # Every claimed midrun/perstep liveness must have either an E4 witness, an E1 witness
        # that a channel exists and the loop reads it, or an explicit author declaration.
        # This is the check behind SPEC 4.3 / AGENT-PROTOCOL gate 4.
        channel_evidence = ("callback", "architecture", "signal-probe", "source")
        for p in self.params:
            if p.affordance == "unresolved" or not p.live:
                continue
            if p.provenance.grade == "E4":
                continue
            if p.provenance.grade == "E0":
                continue
            if p.provenance.grade == "E1" and p.provenance.evidence in channel_evidence:
                continue
            raise SeamError(
                f"{p.name}: liveness={p.liveness!r} claimed at {p.provenance.grade}; mid-run "
                "liveness needs an E4 witness, an E1 witness of a channel the loop reads, or "
                "an explicit E0/assumed declaration (SPEC 4.3). A slider that moves and does "
                "nothing is worse than no slider."
            )


@dataclass
class CrossingRecord:
    """One crossing of the seam. SPEC 6.2."""

    seq: int
    at: str
    kind: str
    point: str
    actor: str
    outcome: str
    value: Any = None
    job_time: dict[str, Any] | None = None
    caused_by: int | None = None
    note: str | None = None

    KINDS = ("seal", "observe", "intervene", "transition", "artifact", "dissent")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seq": self.seq,
            "at": self.at,
            "job_time": self.job_time,
            "kind": self.kind,
            "point": self.point,
            "actor": self.actor,
            "value": self.value,
            "outcome": self.outcome,
            "caused_by": self.caused_by,
        }
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CrossingRecord:
        return cls(
            seq=d["seq"],
            at=d["at"],
            kind=d["kind"],
            point=d["point"],
            actor=d["actor"],
            outcome=d["outcome"],
            value=d.get("value"),
            job_time=d.get("job_time"),
            caused_by=d.get("caused_by"),
            note=d.get("note"),
        )


def asdict(obj: Any) -> dict[str, Any]:
    return dataclasses.asdict(obj)
