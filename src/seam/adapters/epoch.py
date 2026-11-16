"""EPOCH3 adapter — deck-driven CLI, entirely ``sealed``.

Two facts about this particular build, both discovered by running it rather than reading it:

1. It is **interactive**: it prints ``Specify output directory`` and reads a path from stdin,
   then looks for ``<that path>/input.deck``. Feeding the deck itself on stdin makes the first
   block header become the output directory name, and the run fails with a confusing
   ``Input deck file "begin:control/input.deck" does not exist``.
2. Domain bounds live in ``begin:control`` (EPOCH 4 style), not ``begin:boundaries``. Putting
   them in ``boundaries`` produces ``Unrecognised element "x_min"`` warnings and a later
   failure on species boundaries.

Neither is inferable from the deck grammar. Both are E4-grade facts about the build, and both
would be lost if this adapter were written from the manual.
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


DECK_TEMPLATE = """begin:control
  nx = {nx}
  ny = {ny}
  nz = {nz}
  x_min = -4 * micron
  x_max = 4 * micron
  y_min = -4 * micron
  y_max = 4 * micron
  z_min = -6 * micron
  z_max = 6 * micron
  t_end = {t_end} * femto
end:control

begin:boundaries
  bc_x_min = {bc_x_min}
  bc_x_max = {bc_x_max}
  bc_y_min = open
  bc_y_max = open
  bc_z_min = open
  bc_z_max = open
end:boundaries

begin:species
  name = electron
  charge = -1.0
  mass = 1.0
  number_density = 1e26
  temp = 1e5
end:species

begin:output
  dt_snapshot = {dt_snapshot} * femto
  ey = always
end:output
"""

_DEFAULTS = {
    "nx": 32, "ny": 32, "nz": 48,
    "t_end": 400.0,
    "bc_x_min": "open", "bc_x_max": "open",
    "dt_snapshot": 40.0,
}

_WROTE = re.compile(
    r"Wrote\s+(?P<kind>\w+)\s*,\s*(?P<file>\S+)\s+at time\s+(?P<time>\S+)\s+and iteration\s+(?P<iter>\d+)"
)


@register
class EpochAdapter(Adapter):
    id = "epoch"
    label = "EPOCH3D"
    binary = _p("SEAM_EPOCH_BIN", "/opt/epoch/epoch3d/bin/epoch3d")
    mpi_np = 4

    # -- staging ----------------------------------------------------------
    def render(self, ctx: RunContext) -> dict[str, str]:
        vals = dict(_DEFAULTS)
        for p in ctx.manifest.params:
            key = p.adapter_key or p.name
            if key in vals and p.name in ctx.values and ctx.values[p.name] is not None:
                vals[key] = ctx.values[p.name]
        deck = DECK_TEMPLATE.format(**vals)

        run_sh = (
            "#!/usr/bin/env bash\n"
            "set -u\n"
            f'cd {shlex.quote(ctx.workdir)}\n'
            'export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1\n'
            f'printf "%s\\n" {shlex.quote(ctx.workdir)} | '
            f"mpirun -np {self.mpi_np} {self.binary}\n"
            "echo SEAM_EXIT=$?\n"
        )
        return {"input.deck": deck, "run.sh": run_sh}

    def run_command(self, ctx: RunContext) -> str:
        return "chmod +x run.sh && ./run.sh"

    def validate(self, ctx: RunContext) -> list[str]:
        r = ctx.transport.run(f"test -x {shlex.quote(self.binary)} && echo ok")
        if "ok" not in r.out:
            return [f"EPOCH binary not executable: {self.binary}"]
        return []

    # -- observation ------------------------------------------------------
    def parse(self, line: str, ctx: RunContext) -> dict[str, Any]:
        return {}

    def watches(self, ctx: RunContext) -> dict[str, str]:
        """``epoch3d.dat`` is appended once per snapshot write — the only live channel.

        stdout is quiet at default verbosity and the SDF files carry no scalars without a
        reader, so the append-only table is the seam.
        """
        return {"dat": ctx.path("epoch3d.dat")}

    def parse_watch(self, key: str, chunk: str, ctx: RunContext) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in chunk.splitlines():
            m = _WROTE.search(line)
            if not m:
                continue
            out.append({
                "iteration": int(m.group("iter")),
                "time": float(m.group("time")),
                "snapshot_index": int(re.sub(r"\D", "", m.group("file")) or 0),
            })
        return out

    def job_time(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if "iteration" in state:
            return {"step": state["iteration"], "t": state.get("time")}
        return None

    # -- intervention -----------------------------------------------------
    def fire(self, ctx: RunContext, param: Param) -> tuple[str, str | None]:
        if param.name == "stop":
            if not ctx.extra.get("pid"):
                pid = _find_pid(ctx, self.binary)
                if pid is None:
                    return "rejected", "no EPOCH process found"
                ctx.extra["pid"] = pid
            r = ctx.transport.run(f"kill -TERM {ctx.extra['pid']}", timeout=30)
            return ("accepted", "SIGTERM sent; EPOCH writes the current state on exit") if r.ok \
                else ("rejected", r.err.strip()[:200])
        return super().fire(ctx, param)


def _find_pid(ctx: RunContext, pattern: str) -> int | None:
    r = ctx.transport.run(f"pgrep -f {shlex.quote(pattern)} | head -1", timeout=30)
    txt = r.out.strip()
    return int(txt) if txt.isdigit() else None
