"""Projections. SPEC 7: one manifest, two consumers, no divergence.

The human projection binds affordances to widgets; the agent projection binds them to tool
signatures. Neither may add a value, bound, or unit that the manifest does not contain —
which is what makes the divergence check cheap and total.
"""

from __future__ import annotations

from typing import Any

from .model import Manifest, Param

WIDGET = {
    "trigger": "button",
    "dial": "slider",
    "ratchet": "select",
    "gauge": "plot",
    "unresolved": "readonly",
}


def human(m: Manifest, values: dict[str, Any] | None = None, *,
          sealed: bool = False, readonly: bool = False) -> list[dict]:
    """Controls for a human surface. `sealed` disables everything post-seal (incl. read-only)."""
    values = values or {}
    out: list[dict] = []

    for p in m.params:
        disabled = True
        if readonly:
            disabled = True
        elif p.affordance == "unresolved":
            disabled = True
        elif p.liveness == "sealed":
            disabled = sealed
        else:
            disabled = False

        out.append({
            "name": p.name,
            "role": "intervention",
            "affordance": p.affordance,
            "widget": WIDGET[p.affordance],
            "liveness": p.liveness,
            "grade": p.grade,
            "unit": p.unit,
            "doc": p.doc,
            "group": p.group,
            "precision": p.precision,
            "confirm": bool(p.confirm),
            "log": bool(p.domain.log) if p.domain else False,
            "disabled": disabled,
            "value": values.get(p.name, p.default),
            "domain": p.domain.to_dict() if p.domain else None,
            "why_unresolved": p.doc if p.affordance == "unresolved" else None,
            "provenance": p.to_dict()["provenance"],
            "assumed": p.provenance.assumed,
        })

    for o in m.observes:
        out.append({
            "name": o.name,
            "role": "observation",
            "affordance": "gauge",
            "widget": _gauge_widget(o.shape),
            "shape": o.shape,
            "pitch": o.pitch,
            "unit": o.unit,
            "doc": o.doc,
            "group": o.group,
            "precision": o.precision,
            "disabled": True,
            "value": values.get(o.name),
            "provenance": o.to_dict()["provenance"],
        })
    return out


def _gauge_widget(shape: str) -> str:
    return {"scalar": "readout", "series": "line", "grid": "heatmap",
            "table": "table", "log": "stream"}[shape]


def agent(m: Manifest) -> list[dict]:
    """Tool signatures for an agent surface. Unresolved points are not exposed at all."""
    tools: list[dict] = []

    for p in m.params:
        if p.affordance == "unresolved":
            continue
        schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        if p.affordance == "trigger":
            desc = p.doc or f"Fire {p.name}."
            desc += f" [liveness={p.liveness}, grade={p.grade}]"
            if p.transition:
                desc += f" Causes: {p.transition}."
            schema["properties"]["confirm"] = {"type": "boolean", "default": True}
        elif p.affordance == "dial":
            prop: dict[str, Any] = {
                "type": "integer" if (p.domain and p.domain.dtype == "int") else "number",
                "minimum": p.domain.lo,
                "maximum": p.domain.hi,
            }
            if p.domain and p.domain.step:
                prop["multipleOf"] = p.domain.step
            if p.unit:
                prop["description"] = f"unit: {p.unit}"
            schema["properties"][p.name] = prop
            schema["required"] = [p.name]
            desc = p.doc or f"Set {p.name}."
            desc += f" [liveness={p.liveness}, grade={p.grade}]"
        else:  # ratchet
            schema["properties"][p.name] = {
                "enum": list(p.domain.values) if p.domain else [],
            }
            schema["required"] = [p.name]
            desc = p.doc or f"Set {p.name}."
            desc += f" [liveness={p.liveness}, grade={p.grade}]"

        tools.append({
            "name": f"{'do' if p.affordance == 'trigger' else 'set'}_{p.name}",
            "description": desc,
            "input_schema": schema,
            "read_only": False,
            "x_seam": {"liveness": p.liveness, "grade": p.grade, "affordance": p.affordance},
        })

    for o in m.observes:
        tools.append({
            "name": f"read_{o.name}",
            "description": (o.doc or f"Read {o.name}.") + f" [shape={o.shape}, pitch={o.pitch}]",
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "read_only": True,
            "x_seam": {"shape": o.shape, "pitch": o.pitch, "source": o.source},
        })
    return tools


def diff(m: Manifest) -> list[str]:
    """SPEC 7.3: divergence between the two projections is a defect, and it is checkable."""
    problems: list[str] = []

    h = {c["name"]: c for c in human(m)}
    a = {t["name"].split("_", 1)[1] if t["name"].startswith(("set_", "do_", "read_")) else t["name"]: t
         for t in agent(m)}

    h_names = {n for n, c in h.items() if c["role"] == "intervention" and c["affordance"] != "unresolved"}
    a_names = {n for n, t in a.items() if not t["read_only"]}
    for n in sorted(h_names - a_names):
        problems.append(f"{n}: in human projection, absent from agent projection")
    for n in sorted(a_names - h_names):
        problems.append(f"{n}: in agent projection, absent from human projection")

    obs_h = {n for n, c in h.items() if c["role"] == "observation"}
    obs_a = {n for n, t in a.items() if t["read_only"]}
    for n in sorted(obs_h ^ obs_a):
        problems.append(f"{n}: observation present in only one projection")

    for n in sorted(h_names & a_names):
        c, t = h[n], a[n]
        prop = t["input_schema"]["properties"].get(n, {})
        if c["affordance"] == "dial" and c["domain"]:
            if prop.get("minimum") != c["domain"]["lo"] or prop.get("maximum") != c["domain"]["hi"]:
                problems.append(
                    f"{n}: dial bounds differ — human [{c['domain']['lo']}, {c['domain']['hi']}] "
                    f"vs agent [{prop.get('minimum')}, {prop.get('maximum')}]"
                )
        if c["affordance"] == "ratchet" and c["domain"]:
            if list(prop.get("enum", [])) != list(c["domain"].get("values", [])):
                problems.append(f"{n}: ratchet domain differs between projections")
    return problems


def summary(m: Manifest) -> dict[str, Any]:
    by_aff: dict[str, int] = {}
    by_live: dict[str, int] = {}
    for p in m.params:
        by_aff[p.affordance] = by_aff.get(p.affordance, 0) + 1
        if p.liveness:
            by_live[p.liveness] = by_live.get(p.liveness, 0) + 1
    return {
        "params": len(m.params),
        "observes": len(m.observes),
        "artifacts": len(m.artifacts),
        "by_affordance": by_aff,
        "by_liveness": by_live,
        "unresolved": [p.name for p in m.params if p.affordance == "unresolved"],
        "assumed": [p.name for p in m.params if p.provenance.assumed and p.live],
        "assumed_bounds": [p.name for p in m.params if p.provenance.assumed and not p.live],
        "profile": m.profile,
    }
