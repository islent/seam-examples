"""The crossing loop.

Seal → launch → poll → observe → intervene → finish. Everything the runtime knows about a
code comes from its adapter; everything it knows about the surface comes from the manifest;
everything that happens goes into the trace.

This is the module that makes the spec executable, and the place where the honest-liveness
rule is enforced: a write to a ``sealed`` parameter is *rejected and recorded*, not silently
dropped on the floor.

Two engineering decisions matter more than they look:

* **One round trip per tick.** Log delta, watched files, directory listing and the liveness
  probe are fetched by a single shell command with section markers. On an SSH seam the naive
  version costs four connections per tick, which on a ten-second job means you observe it
  once. One round trip is what makes the surface actually live.
* **Every appended line is its own crossing.** A code that appends a step table is emitting a
  series, not a state; collapsing it to the latest value throws away the thing you wanted to
  see.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import adapter as adapter_mod
from . import record as record_mod
from .model import Manifest, SeamError
from .record import Trace
from .transport import Transport

MAX_SERIES = 2000  # kept in memory for the surface; the trace on disk keeps everything

_MARK = "@@SEAM:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class RunState:
    status: str = "created"          # created|sealed|running|finished|failed
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    log_offset: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    series: dict[str, list[Any]] = field(default_factory=dict)
    steps: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "started_at": self.started_at,
            "finished_at": self.finished_at, "exit_code": self.exit_code,
            "pid": self.pid, "steps": self.steps, "error": self.error,
            "notes": self.notes, "values": self.values, "series": self.series,
        }


class SeamRun:
    """One job behind one seam."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        transport: Transport,
        workdir: str,
        values: dict[str, Any] | None = None,
        run_id: str | None = None,
        decimate: int = 1,
        actor: str = "seam",
        runs_dir: str | Path | None = None,
    ):
        self.manifest = manifest
        self.transport = transport
        self.workdir = workdir.rstrip("/")
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.adapter = adapter_mod.get(manifest.adapter_id)
        self.decimate = max(1, decimate)
        self.state = RunState()
        self._observe_names = {o.name for o in manifest.observes}
        self._sample_count = 0
        self._seen_artifacts: set[str] = set()
        self._empty_polls = 0

        self.values: dict[str, Any] = {}
        for p in manifest.params:
            if p.affordance in ("unresolved", "trigger"):
                continue
            self.values[p.name] = (values or {}).get(p.name, p.default)

        # The trace lives locally, next to the surface. It is the client-side record of the
        # seam, and it must survive a cluster directory being cleaned up.
        root = Path(runs_dir) if runs_dir else Path.cwd() / "runs"
        self.local_dir = (root / self.run_id).resolve()
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.trace = Trace(self.local_dir / "trace.jsonl", manifest, actor=actor)

        self.ctx = adapter_mod.RunContext(
            transport=transport, trace=self.trace, manifest=manifest, workdir=self.workdir,
            values=self.values, extra={"run_id": self.run_id},
        )
        self.poll_seconds = _parse_duration(str(manifest.cadence.get("poll", "1s")))

    # ------------------------------------------------------------------
    # seal
    # ------------------------------------------------------------------
    def seal(self) -> None:
        # A sealed value is still a declared value: if it sits outside its domain, the manifest
        # is being contradicted at submission time and every surface will render an impossible
        # number. Caught by seeing "1.20e3 a/c" on a dial declared [5, 500].
        for p in self.manifest.params:
            if p.affordance in ("unresolved", "trigger") or p.domain is None:
                continue
            v = self.values.get(p.name)
            if v is None or p.domain.kind == "void":
                continue
            if not p.domain.admits(v):
                raise SeamError(
                    f"{p.name} = {v!r} is outside its declared domain "
                    f"{p.domain.to_dict()}. Sealing it would put a value in the manifest's "
                    "own surface that the manifest says is impossible."
                )
        problems = self.adapter.validate(self.ctx)
        if problems:
            raise SeamError("pre-flight failed:\n  " + "\n  ".join(problems))

        self.transport.mkdirp(self.workdir)
        for name, text in self.adapter.render(self.ctx).items():
            self.ctx.write(name, text)

        if self.decimate > 1:
            self.trace.note(
                "observe",
                f"observe records decimated 1:{self.decimate} by the runtime; declared here so "
                "a reader can tell a plateau from a gap (SPEC 11.1)",
            )
        self.trace.seal(dict(self.values))
        self.state.status = "sealed"
        self.state.values = dict(self.values)

    # ------------------------------------------------------------------
    # launch
    # ------------------------------------------------------------------
    def launch(self) -> None:
        if self.state.status == "created":
            self.seal()
        cmd = self.adapter.run_command(self.ctx)
        log = shlex.quote(self.ctx.path(self.ctx.log_file))
        pidfile = shlex.quote(self.ctx.path(".seam.pid"))
        # The *whole compound* is backgrounded and redirected. Redirecting only the last
        # command leaves the background subshell holding ssh's stdout pipe, and ssh then
        # blocks until the job finishes — which on a short run means the surface never sees
        # it running at all.
        # No `exec`: the shell must stay alive so its pid stays meaningful for `kill -0`,
        # and `exec a && b` would replace the shell with `a` and silently drop `b`.
        inner = f"echo $$ > {pidfile}; {cmd}"
        wrapper = (
            f"( cd {shlex.quote(self.workdir)} && rm -f {pidfile} && "
            f"setsid bash -c {shlex.quote(inner)} ) > {log} 2>&1 < /dev/null & echo $!"
        )
        r = self.transport.run(wrapper, timeout=60)
        self.state.pid = self._read_pid(pidfile)
        self.state.status = "running"
        self.state.started_at = _now_iso()
        self.trace.transition("running")
        if self.state.pid is None:
            self.state.notes.append("no pid available; liveness falls back to log silence")

    def _read_pid(self, quoted_pidfile: str) -> int | None:
        """The setsid'd shell writes its own pid; ``$!`` is the short-lived subshell."""
        for _ in range(10):
            r = self.transport.run(f"cat {quoted_pidfile} 2>/dev/null", timeout=20)
            txt = (r.out or "").strip()
            if txt.isdigit():
                return int(txt)
            time.sleep(0.15)
        return None

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def tick(self) -> bool:
        """One crossing pass. Returns True while the job is alive."""
        if self.state.status not in ("running", "sealed"):
            return False

        bundle = self._poll_bundle()
        self._drain(bundle)

        if bundle["alive"]:
            return True

        # Whatever landed after the process exited.
        self._drain(self._poll_bundle())
        self._finish()
        return False

    def wait(self, *, timeout: float | None = None,
             on_tick: Callable[[SeamRun], None] | None = None) -> RunState:
        deadline = time.time() + timeout if timeout else None
        while self.state.status in ("running", "sealed"):
            if deadline and time.time() > deadline:
                self.state.notes.append("wait() timed out; job left running")
                break
            if on_tick:
                on_tick(self)
            if not self.tick():
                break
            time.sleep(self.poll_seconds)
        return self.state

    def _drain(self, bundle: dict[str, Any]) -> None:
        for line in bundle["log"].splitlines():
            self._ingest(self.adapter.parse(line, self.ctx))
        for key, chunk in bundle["watches"].items():
            if not chunk:
                continue
            for obs in self.adapter.parse_watch(key, chunk, self.ctx):
                self._ingest(obs)
        self._ingest(self.adapter.poll_files(self.ctx, self.state) or {})
        self._sync_artifacts(bundle["listing"])
        self._write_state_file()

    # ------------------------------------------------------------------
    # intervention
    # ------------------------------------------------------------------
    def apply(self, name: str, value: Any, *, actor: str = "human") -> dict[str, Any]:
        try:
            p = self.manifest.param(name)
        except SeamError:
            return {"point": name, "outcome": "rejected", "note": "no such intervention point"}

        if self.state.status not in ("running", "sealed", "created"):
            # A rejection is a crossing too: recording it is what lets a reader tell "the
            # operator never tried" from "the operator tried after the run ended".
            rec = self.trace.intervene(
                name, value, "rejected", actor=f"human:{actor}",
                note=f"job is {self.state.status}: the write never reached the seam",
            )
            return rec.to_dict()

        if p.domain is not None and p.domain.kind != "void" and not p.domain.admits(value):
            rec = self.trace.intervene(
                name, value, "rejected", actor=f"human:{actor}",
                note=f"outside declared domain {p.domain.to_dict()}: rejected at the seam, not "
                     "passed to the job (SPEC 7.2)",
            )
            return rec.to_dict()

        outcome, note = self.adapter.apply(self.ctx, p, p.encode(value))
        rec = self.trace.intervene(name, value, outcome, actor=f"human:{actor}", note=note,
                                   job_time=self.adapter.job_time(self.state.values))
        if outcome == "accepted":
            self.state.values[name] = value
        self._write_state_file()
        return rec.to_dict()

    def stop(self) -> dict[str, Any]:
        for p in self.manifest.params:
            if p.affordance == "trigger" and p.name in ("stop", "terminate", "kill"):
                return self.apply(p.name, True, actor="seam")
        if self.state.pid:
            r = self.transport.run(f"kill -TERM {self.state.pid}", timeout=30)
            rec = self.trace.intervene(
                "stop", None, "accepted" if r.ok else "rejected", actor="human:seam",
                note="no declared stop trigger; SIGTERM sent directly (undeclared channel)",
            )
            return rec.to_dict()
        return {"point": "stop", "outcome": "rejected", "note": "no pid and no stop trigger"}

    # ------------------------------------------------------------------
    # one round trip
    # ------------------------------------------------------------------
    def _poll_bundle(self) -> dict[str, Any]:
        watches = self.adapter.watches(self.ctx) or {}
        parts: list[str] = []
        parts.append(
            f"echo '{_MARK}ALIVE' ; "
            + (f"kill -0 {self.state.pid} 2>/dev/null && echo 1 || echo 0"
               if self.state.pid else "echo 1"))
        parts.append(f"echo '{_MARK}LS' ; ls -1 {shlex.quote(self.workdir)} 2>/dev/null")
        for key, path in watches.items():
            off = self.ctx.offsets.get(key, 0)
            parts.append(f"echo '{_MARK}W:{key}' ; "
                         f"tail -c +{off + 1} {shlex.quote(path)} 2>/dev/null")
        parts.append(f"echo '{_MARK}LOG' ; "
                     f"tail -c +{self.state.log_offset + 1} "
                     f"{shlex.quote(self.ctx.path(self.ctx.log_file))} 2>/dev/null")

        r = self.transport.run(" ; ".join(parts), timeout=90)
        sections = _split_sections(r.out or "")

        log = sections.get("LOG", "")
        self.state.log_offset += len(log.encode())

        new_watches: dict[str, str] = {}
        for key in watches:
            chunk = sections.get(f"W:{key}", "")
            if chunk:
                self.ctx.offsets[key] = self.ctx.offsets.get(key, 0) + len(chunk.encode())
            new_watches[key] = chunk

        listing = set((sections.get("LS") or "").split())
        alive = (sections.get("ALIVE") or "").strip().endswith("1")
        if not self.state.pid:
            if log.strip() or listing:
                self._empty_polls = 0
            else:
                self._empty_polls += 1
                alive = self._empty_polls < 2
        return {"log": log, "watches": new_watches, "listing": listing, "alive": alive}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _ingest(self, obs: dict[str, Any]) -> None:
        if not obs:
            return
        for name, value in obs.items():
            if name in self._observe_names:
                self.state.values[name] = value
        for name, value in obs.items():
            self._record_observation(name, value)

    def _record_observation(self, name: str, value: Any) -> None:
        if name not in self._observe_names:
            return
        self._sample_count += 1
        if self.decimate > 1 and self._sample_count % self.decimate != 0:
            return
        self.state.values[name] = value
        if name in ("step", "iteration") and isinstance(value, (int, float)):
            self.state.steps = int(value)
        series = self.state.series.setdefault(name, [])
        series.append(value)
        if len(series) > MAX_SERIES:
            del series[: len(series) - MAX_SERIES]
        self.trace.observe(name, value, job_time=self.adapter.job_time(self.state.values))

    def _sync_artifacts(self, listing: set[str]) -> None:
        if not self.manifest.artifacts:
            return
        for a in self.manifest.artifacts:
            if a.name in self._seen_artifacts:
                continue
            if _artifact_present(a.path, listing):
                self._seen_artifacts.add(a.name)
                self.trace.artifact(a.name, note=a.path)

    def _finish(self) -> None:
        self.adapter.finish(self.ctx, self.state.values)
        self.state.status = "finished"
        self.state.finished_at = _now_iso()
        self.trace.transition("finished", outcome="exited")
        self._write_state_file()
        self.trace.close()

    def _write_state_file(self) -> None:
        blob = {
            "run_id": self.run_id, "workdir": self.workdir,
            "identity": self.manifest.identity, "state": self.state.to_dict(),
            "updated_at": _now_iso(),
        }
        try:
            self.transport.put(json.dumps(blob, ensure_ascii=False, indent=2),
                               self.ctx.path(self.ctx.state_file))
        except Exception:
            pass  # convenience for humans on the target, never load-bearing

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        from . import projection

        return {
            "run_id": self.run_id, "workdir": self.workdir,
            "transport": self.transport.name, "identity": self.manifest.identity,
            "profile": self.manifest.profile, "status": self.state.status,
            "controls": projection.human(self.manifest, self.state.values,
                                         sealed=self.state.status != "created"),
            "summary": projection.summary(self.manifest),
            "values": self.state.values, "series": self.state.series,
            "steps": self.state.steps, "pid": self.state.pid,
            "notes": self.state.notes, "error": self.state.error,
            "records": _tail_records(self.trace.path),
            "trace": str(self.trace.path),
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _split_sections(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        if line.startswith(_MARK):
            cur = line[len(_MARK):].strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _tail_records(path: Path, n: int = 120) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("_header"):
                continue
            rows.append(d)
    return rows[-n:]


def _artifact_present(pattern: str, names: set[str]) -> bool:
    import re

    name = pattern.split("/")[-1]
    if "*" in name:
        import fnmatch

        return any(fnmatch.fnmatch(n, name) for n in names)
    if "%" not in name:
        return name in names
    rx = re.sub(r"%0?\d*d", r"\\d+", re.escape(name))
    try:
        compiled = re.compile("^" + rx + "$")
    except re.error:
        return False
    return any(compiled.match(n) for n in names)


def _parse_duration(text: str) -> float:
    text = text.strip()
    if text.endswith("ms"):
        return float(text[:-2]) / 1000
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def open_existing(workdir: str | Path) -> dict[str, Any]:
    path = Path(workdir) / "trace.jsonl"
    if not path.exists():
        raise SeamError(f"no trace at {path}")
    return record_mod.latest(path)
