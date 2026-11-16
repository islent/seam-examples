"""Crossing trace. SPEC 6: append-only, causal, total.

Every crossing leaves a record — observations included. That is the design decision that
makes a trace answer "what did the operator see before they intervened" instead of only
"what did the operator do".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .model import CrossingRecord, Manifest


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class Trace:
    """Append-only crossing trace.

    Writes JSONL as it goes, so a crashed run leaves a usable trace rather than an
    in-memory structure that dies with the process.
    """

    def __init__(self, path: str | Path, manifest: Manifest, *, actor: str = "seam"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.actor = actor
        self.seq = 0
        self._last_observe: dict[str, int] = {}
        self._last_seq = 0
        self._fh = self.path.open("a", encoding="utf-8")
        if self.path.stat().st_size == 0:
            header = {
                "_header": True,
                "seam_version": manifest.version,
                "manifest_sha256": manifest_hash(manifest),
                "identity": manifest.identity,
                "opened_at": _now(),
            }
            self._fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            self._fh.flush()

    # -- mutation ---------------------------------------------------------
    def _append(self, rec: CrossingRecord) -> CrossingRecord:
        self._fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()
        self._last_seq = rec.seq
        return rec

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def seal(self, values: dict[str, Any]) -> CrossingRecord:
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="seal", point="params", actor=self.actor,
            outcome="accepted", value=values, caused_by=None,
        ))

    def transition(self, state: str, outcome: str = "entered",
                   job_time: dict[str, Any] | None = None) -> CrossingRecord:
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="transition", point=state, actor="job",
            outcome=outcome, job_time=job_time, caused_by=self._last_seq or None,
        ))

    def observe(self, point: str, value: Any, job_time: dict[str, Any] | None = None,
                caused_by: int | None = None) -> CrossingRecord:
        rec = self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="observe", point=point, actor=self.actor,
            outcome="sampled", value=value, job_time=job_time,
            caused_by=caused_by if caused_by is not None else self._last_seq or None,
        ))
        self._last_observe[point] = rec.seq
        return rec

    def intervene(self, point: str, value: Any, outcome: str,
                  *, actor: str | None = None, note: str | None = None,
                  job_time: dict[str, Any] | None = None) -> CrossingRecord:
        # caused_by: the most recent observation, if any. This is the link that makes the
        # trace answer "what did they see before they intervened" (SPEC 6.2).
        cause = max(self._last_observe.values()) if self._last_observe else None
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="intervene", point=point,
            actor=actor or f"human:{self.actor}", outcome=outcome, value=value,
            job_time=job_time, caused_by=cause, note=note,
        ))

    def artifact(self, name: str, ref: str | None = None, note: str | None = None) -> CrossingRecord:
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="artifact", point=name, actor="job",
            outcome="observed", value={"ref": ref} if ref else None, caused_by=self._last_seq or None,
            note=note,
        ))

    def dissent(self, point: str, claim: Any, note: str) -> CrossingRecord:
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="dissent", point=point, actor=self.actor,
            outcome="rejected", value=claim, caused_by=self._last_seq or None, note=note,
        ))

    def note(self, point: str, note: str) -> CrossingRecord:
        return self._append(CrossingRecord(
            seq=self._next(), at=_now(), kind="dissent", point=point, actor=self.actor,
            outcome="observed", value=None, caused_by=self._last_seq or None, note=note,
        ))

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Trace:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------


def read(path: str | Path) -> tuple[dict[str, Any], list[CrossingRecord]]:
    records: list[CrossingRecord] = []
    header: dict[str, Any] = {}
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("_header"):
                header = d
                continue
            records.append(CrossingRecord.from_dict(d))
    return header, records


def iter_records(path: str | Path) -> Iterator[CrossingRecord]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("_header"):
                continue
            yield CrossingRecord.from_dict(d)


def latest(path: str | Path) -> dict[str, Any]:
    """Replay a trace into the final observation state. Used by the UI and by specimens."""
    header, records = read(path)
    state: dict[str, Any] = {}
    series: dict[str, list[Any]] = {}
    interventions: list[dict[str, Any]] = []
    for r in records:
        if r.kind == "observe":
            state[r.point] = r.value
            series.setdefault(r.point, []).append(r.value)
        elif r.kind == "intervene":
            interventions.append({"seq": r.seq, "point": r.point, "value": r.value,
                                  "outcome": r.outcome, "caused_by": r.caused_by})
    return {"header": header, "state": state, "series": series,
            "interventions": interventions, "count": len(records)}


def manifest_hash(m: Manifest) -> str:
    blob = json.dumps(m.to_dict(), sort_keys=True, ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()
