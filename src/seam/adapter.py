"""Adapter protocol.

An adapter answers four questions about one code:

1. Given sealed values, what do I write to disk?  → ``render``
2. What shell command starts the job?             → ``run_command``
3. Given a log line, what did I observe?          → ``parse``
4. Given a write, what does the code actually do? → ``apply``

It does **not** decide affordances. The manifest already did (via derivation), and the
adapter must honour what the manifest claims — including the claim that a parameter is
``sealed``, in which case ``apply`` must reject rather than pretend.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .model import Manifest, Param, SeamError
from .record import Trace
from .transport import Transport


@dataclass
class RunContext:
    transport: Transport
    trace: Trace
    manifest: Manifest
    workdir: str                       # absolute path on the target
    values: dict[str, Any] = field(default_factory=dict)   # sealed values, display units
    log_file: str = "job.log"
    ctrl_file: str = "seam.ctrl"
    state_file: str = "seam.state"
    extra: dict[str, Any] = field(default_factory=dict)
    #: per-watch byte offsets, so a growing file is read as a series and not re-read
    offsets: dict[str, int] = field(default_factory=dict)

    def path(self, name: str) -> str:
        return f"{self.workdir.rstrip('/')}/{name}"

    def write(self, name: str, text: str) -> str:
        p = self.path(name)
        self.transport.put(text, p)
        return p

    def append_ctrl(self, line: str) -> None:
        """Append a command for the job's own loop to pick up (the mid-run channel)."""
        cmd = f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(self.path(self.ctrl_file))}"
        r = self.transport.run(cmd, timeout=30)
        if not r.ok:
            raise RuntimeError(f"control write failed: {r.err.strip()}")


class Adapter:
    id: ClassVar[str] = "base"
    label: ClassVar[str] = "generic"
    #: Shell command template, relative to the work directory. ``{workdir}`` is substituted.
    command: ClassVar[str] = "./run.sh"

    # -- staging ----------------------------------------------------------
    def render(self, ctx: RunContext) -> dict[str, str]:
        """Return {filename: contents} to be written into the work directory."""
        raise NotImplementedError

    def run_command(self, ctx: RunContext) -> str:
        return self.command

    # -- observation ------------------------------------------------------
    def parse(self, line: str, ctx: RunContext) -> dict[str, Any]:
        """Return {observation_name: value} for whatever this line carries."""
        return {}

    def job_time(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if not state:
            return None
        jt: dict[str, Any] = {}
        if "step" in state:
            jt["step"] = state["step"]
        if "time" in state:
            jt["t"] = state["time"]
        return jt or None

    def poll_files(self, ctx: RunContext, state: dict[str, Any]) -> dict[str, Any]:
        """Read scalar, file-backed observations (the latest value only)."""
        return {}

    def watches(self, ctx: RunContext) -> dict[str, str]:
        """Files to read incrementally, as ``{key: absolute path}``.

        The runtime fetches only the bytes appended since the last tick, so a step table that
        grows is turned into one crossing per appended line rather than a single lump.
        """
        return {}

    def parse_watch(self, key: str, chunk: str, ctx: RunContext) -> list[dict[str, Any]]:
        """Turn a freshly appended chunk of a watched file into one observation batch per event."""
        return []

    # -- intervention -----------------------------------------------------
    def apply(self, ctx: RunContext, param: Param, value: Any) -> tuple[str, str | None]:
        """Return (outcome, note). Outcomes: accepted | deferred | rejected."""
        if param.liveness == "sealed":
            return "rejected", (
                "sealed after submission: this code reads the value once at start-up, so a "
                "write here would be accepted and ignored (SPEC 4.3)"
            )
        if param.affordance == "trigger":
            return self.fire(ctx, param)
        return self.set_live(ctx, param, value)

    def fire(self, ctx: RunContext, param: Param) -> tuple[str, str | None]:
        return "rejected", f"{param.name}: adapter has no implementation for this trigger"

    def set_live(self, ctx: RunContext, param: Param, value: Any) -> tuple[str, str | None]:
        return "rejected", f"{param.name}: adapter declares liveness but has no live channel"

    # -- lifecycle --------------------------------------------------------
    def finish(self, ctx: RunContext, state: dict[str, Any]) -> None:
        """Collection hook: read final values, close artifacts."""

    def validate(self, ctx: RunContext) -> list[str]:
        """Pre-flight: reasons this job cannot run. Empty list means go."""
        return []


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

_ADAPTERS: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    _ADAPTERS[cls.id] = cls
    return cls


def get(adapter_id: str) -> Adapter:
    if adapter_id not in _ADAPTERS:
        _bootstrap()
    if adapter_id not in _ADAPTERS:
        raise SeamError(
            f"unknown adapter {adapter_id!r}; known: {sorted(_ADAPTERS)}. "
            "An adapter is what makes a manifest executable; the manifest alone is a claim."
        )
    return _ADAPTERS[adapter_id]()


_BOOTSTRAPPED = False


def _bootstrap() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    from .adapters import astronbodysim, epoch, gadget2, meep, postproc  # noqa: F401
