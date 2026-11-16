"""Gadget-2 adapter — tag-value paramfile CLI, entirely ``sealed``.

The paramfile grammar is ``Tag<whitespace>Value`` and this adapter *writes* it that way. Using
``Tag = Value`` makes ``=`` the value: every subsequent ``strcpy`` stores ``"="`` and the run
dies with ``cp: cannot create regular file '=/parameters-usedvalues'``.

The unit block is written by the template, not by the manifest, because it defines the
meaning of every other number. ``UnitTime = UnitLength/UnitVelocity`` = 0.978 Gyr for this
choice, so ``TimeMax`` in code units is not Gyr — the manifest carries ``display_scale`` and
the adapter writes the converted value.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from ..adapter import Adapter, RunContext, register
from ..model import Param


def _p(env: str, default: str) -> str:
    """Where the code lives: environment first, else a conventional prefix."""
    return os.environ.get(env, default)


_DEFAULTS = {
    "InitCondFile": "ic_plummer.dat",
    "BoxSize": 300.0,
    "Omega0": 0.3,
    "OmegaBaryon": 0.045,
    "OmegaLambda": 0.7,
    "HubbleParam": 0.7,
    "TimeMax": 0.02,
    "TimeBetSnapshot": 0.01,
    "ErrTolIntAccuracy": 0.025,
    "ErrTolTheta": 0.5,
    "ErrTolForceAcc": 0.005,
    "SofteningHalo": 0.05,
    "SofteningHaloMaxPhys": 0.05,
}


@register
class Gadget2Adapter(Adapter):
    id = "gadget2"
    label = "Gadget-2.0.7"
    binary = _p("SEAM_GADGET2_BIN", "/opt/gadget2/bin/Gadget2_tree_double")
    ic_source = _p("SEAM_GADGET2_IC", "/opt/gadget2/runs/test_tree/ic_plummer.dat")
    #: The code's own reference paramfile. Rewriting only the declared tags is safer than
    #: inventing a paramfile: this build demands ~20 tags that no manifest would list, and a
    #: missing one aborts the run with "Error. I miss a value for tag ...".
    base_paramfile = _p("SEAM_GADGET2_PARAM",
                        "/opt/gadget2/runs/test_tree/param_test.txt")

    def render(self, ctx: RunContext) -> dict[str, str]:
        vals = dict(_DEFAULTS)
        for p in ctx.manifest.params:
            key = p.adapter_key or p.name
            if key in vals and p.name in ctx.values and ctx.values[p.name] is not None:
                vals[key] = ctx.values[p.name]
        # display units -> code units. TimeMax is the one that cannot be skipped.
        for p in ctx.manifest.params:
            if p.display_scale and p.name in ctx.values and ctx.values[p.name] is not None:
                key = p.adapter_key or p.name
                if key in vals:
                    vals[key] = float(ctx.values[p.name]) / p.display_scale

        base = ctx.transport.get(self.base_paramfile)
        if base is None:
            raise RuntimeError(f"reference paramfile missing on target: {self.base_paramfile}")
        text = _rewrite_paramfile(base.decode("utf-8", "replace"), vals)
        run_sh = (
            "#!/usr/bin/env bash\n"
            "set -u\n"
            f"cd {shlex.quote(ctx.workdir)}\n"
            "mkdir -p output\n"
            f"{self.binary} param.txt\n"
            "echo SEAM_EXIT=$?\n"
        )
        files = {"param.txt": text, "run.sh": run_sh}
        ic = ctx.transport.get(self.ic_source)
        if ic:
            ctx.transport.put(ic, ctx.path("ic_plummer.dat"))
        return files

    def run_command(self, ctx: RunContext) -> str:
        return "chmod +x run.sh && ./run.sh"

    def validate(self, ctx: RunContext) -> list[str]:
        problems = []
        r = ctx.transport.run(f"test -x {shlex.quote(self.binary)} && echo ok")
        if "ok" not in r.out:
            problems.append(f"Gadget-2 binary not executable: {self.binary}")
        r = ctx.transport.run(f"test -e {shlex.quote(self.ic_source)} && echo ok")
        if "ok" not in r.out:
            problems.append(f"initial conditions not found: {self.ic_source}")
        return problems

    # -- observation ------------------------------------------------------
    def watches(self, ctx: RunContext) -> dict[str, str]:
        """energy.txt is appended every TimeBetStatistics — one row per statistics write."""
        return {"energy": ctx.path("output/energy.txt")}

    def parse_watch(self, key: str, chunk: str, ctx: RunContext) -> list[dict[str, Any]]:
        """Columns, as observed on this build: Time  a  EnergyPot  EnergyKin  ...

        The order is established by observation (E4), not by the manual: the documented
        ordering does not match what this source drop writes, and E4 is the class that wins.
        """
        out: list[dict[str, Any]] = []
        zero_run = 0
        for line in chunk.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                time_, a, epot, ekin = (float(x) for x in parts[:4])
            except ValueError:
                continue
            out.append({"time": time_, "scale_factor": a,
                        "energy_pot": epot, "energy_kin": ekin})
            zero_run = zero_run + 1 if epot == 0.0 else 0

        # A plausible-looking file with a silently zero column: -DOUTPUTPOTENTIAL missing.
        if zero_run >= 3 and not ctx.extra.get("epot_zero_noted"):
            ctx.extra["epot_zero_noted"] = True
            ctx.trace.dissent(
                "energy_pot", {"energy_pot": 0.0},
                "EnergyPot column is identically zero over consecutive rows: the build likely "
                "lacks -DOUTPUTPOTENTIAL, which makes the column silently 0 (run.c:217). The "
                "value is not wrong, it is absent.",
            )
        return out

    def job_time(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if "time" in state:
            return {"t": state["time"]}
        return None

    def finish(self, ctx: RunContext, state: dict[str, Any]) -> None:
        # Gadget-2 normally leaves this line behind; it reads like a failure and is not one.
        r = ctx.transport.run(
            f"grep -c \"cannot stat 'output/restart\" {shlex.quote(ctx.path(ctx.log_file))} 2>/dev/null",
            timeout=30,
        )
        if r.out.strip() not in ("", "0"):
            ctx.trace.note("restart", "harmless 'mv: cannot stat output/restart.N' on exit "
                                      "(ResubmitOn=0 rename path)")

    # -- intervention -----------------------------------------------------
    def fire(self, ctx: RunContext, param: Param) -> tuple[str, str | None]:
        if param.name == "stop":
            r = ctx.transport.run(
                f"pgrep -f {shlex.quote(self.binary)} | head -1", timeout=30)
            pid = r.out.strip()
            if not pid.isdigit():
                return "rejected", "no Gadget-2 process found"
            k = ctx.transport.run(f"kill -TERM {pid}", timeout=30)
            return ("accepted", "SIGTERM sent; a restart file is written") if k.ok \
                else ("rejected", k.err.strip()[:200])
        return super().fire(ctx, param)


_KV = re.compile(r"^(\s*)([A-Za-z_]\w*)(\s+)(\S.*?)(\s*)$")


def _rewrite_paramfile(text: str, values: dict[str, Any]) -> str:
    """Rewrite only the tags we manage, keeping the rest of the file byte-for-byte.

    Two rules, both from the reader rather than the file:
      * a line is ``Tag<whitespace>Value`` — never ``Tag = Value`` (``sscanf("%s%s%s")`` makes
        ``=`` the value, and every later strcpy inherits it);
      * comments are ``%``-prefixed and are preserved, because they carry the unit block.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _KV.match(line)
        if not m or line.lstrip().startswith("%"):
            out.append(line)
            continue
        _indent, tag, sep, _old, _tail = m.groups()
        if tag in values:
            value = values[tag]
            if isinstance(value, float):
                text_value = f"{value:.6g}"
            elif isinstance(value, bool):
                text_value = "1" if value else "0"
            else:
                text_value = str(value)
            out.append(f"{tag:<26}{text_value}")
            seen.add(tag)
        else:
            out.append(line)
    for tag, value in values.items():
        if tag not in seen:
            out.append(f"{tag:<26}{value}")
    header = "% written by seam: Tag<whitespace>Value only; '=' would become the value\n"
    return header + "\n".join(out) + "\n"
