"""Transports. SPEC 2(b): Seam does not define a wire format; this is one of many.

Two implementations ship: local (same machine) and ssh (a remote host, used here to reach
an ssh-able cluster node). Both are deliberately dumb — they run shell strings and move bytes.
Anything cleverer belongs in an adapter, not here.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Transport:
    name: str = "transport"

    def run(self, cmd: str, *, timeout: float | None = 60, stdin: str | None = None) -> Result:
        raise NotImplementedError

    def put(self, data: bytes | str, remote: str) -> None:
        raise NotImplementedError

    def get(self, remote: str) -> bytes | None:
        raise NotImplementedError

    def exists(self, remote: str) -> bool:
        raise NotImplementedError

    def mkdirp(self, remote: str) -> None:
        r = self.run(f"mkdir -p {shlex.quote(remote)}")
        if not r.ok:
            raise RuntimeError(f"mkdir -p {remote} failed: {r.err.strip()}")

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


class LocalTransport(Transport):
    name = "local"

    def run(self, cmd: str, *, timeout: float | None = 60, stdin: str | None = None) -> Result:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            input=stdin, executable=_shell(),
        )
        return Result(r.returncode, r.stdout, r.stderr)

    def put(self, data: bytes | str, remote: str) -> None:
        p = Path(remote)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data, encoding="utf-8")
        else:
            p.write_bytes(data)

    def get(self, remote: str) -> bytes | None:
        p = Path(remote)
        return p.read_bytes() if p.exists() else None

    def exists(self, remote: str) -> bool:
        return Path(remote).exists()


class SshTransport(Transport):
    """Run everything through ``ssh <host> <cmd>``, in batch mode.

    Binary-safe: puts go through ``cat > path`` with the bytes on stdin, gets through
    ``cat path`` on stdout. No scp, no sftp, no paramiko — one dependency fewer and it
    works on Windows where the OpenSSH client is already present.
    """

    def __init__(self, host: str, *, connect_timeout: int = 10, extra_opts: list[str] | None = None):
        self.host = host
        self.opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
        self.opts += extra_opts or []

    name = "ssh"

    def _argv(self, cmd: str) -> list[str]:
        return ["ssh", *self.opts, self.host, cmd]

    def run(self, cmd: str, *, timeout: float | None = 120, stdin: str | None = None) -> Result:
        r = subprocess.run(
            self._argv(cmd), capture_output=True, text=True, timeout=timeout, input=stdin,
        )
        return Result(r.returncode, r.stdout, r.stderr)

    def put(self, data: bytes | str, remote: str) -> None:
        payload = data.encode() if isinstance(data, str) else data
        parent = os.path.dirname(remote) or "."
        cmd = f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(remote)}"
        r = subprocess.run(self._argv(cmd), input=payload, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"put {remote} failed: {r.stderr.decode(errors='replace').strip()}")

    def get(self, remote: str) -> bytes | None:
        r = subprocess.run(self._argv(f"cat {shlex.quote(remote)} 2>/dev/null"),
                           capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def exists(self, remote: str) -> bool:
        r = self.run(f"test -e {shlex.quote(remote)} && echo 1", timeout=30)
        return r.out.strip() == "1"


def _shell() -> str | None:
    if sys.platform.startswith("win"):
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(cand).exists():
                return cand
        return shutil.which("bash") or None
    return None


def make(kind: str = "local", host: str | None = None) -> Transport:
    if kind == "ssh":
        if not host:
            raise ValueError("ssh transport needs a host, e.g. --host user@hpc-node")
        return SshTransport(host)
    return LocalTransport()
