"""HTTP surface for the three modes, plus the channels a desktop binding needs.

Deliberately tiny and stdlib-only: the point being demonstrated is that a Seam surface is a
*projection of the manifest*, not a framework. Anything that can serve JSON, a byte stream,
and an event stream can host Bench, Scenario, and Specimen — a browser, an ImGui window, or
an Electron shell all talk to the same three endpoints.

Endpoints
    GET  /                      the surface (ui/index.html)
    GET  /api/manifest          manifest + both projections + derivation trace
    GET  /api/state             current controls, series, status
    GET  /api/events            server-sent events: one `state` frame per crossing batch
    GET  /api/frame?point=&w=   a grid observation as a PNG preview (SPEC 4.6)
    GET  /api/scenarios         parsed scenarios that target this manifest
    GET  /api/scenario?name=…   one parsed scenario
    POST /api/intervene         {point, value} -> crossing record
    POST /api/stop              stop the job
    POST /api/bench             {point, patch} -> re-derived point + projection diff

Why SSE and not polling: the transport to the job is an SSH round trip of order a second, so
the crossing loop is already the bottleneck. A client that polls at 1.5 s adds 1.5 s of dead
time on top of it for no reason, and a client that polls at 100 ms just asks the same question
fifteen times. SSE pushes exactly when there is news, and costs nothing when there is not.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import projection
from .model import Manifest, SeamError


class Surface:
    """What the UI talks to: a live run, or a specimen. Same shape either way."""

    def __init__(self, manifest: Manifest, *, run=None, specimen: dict | None = None,
                 scenario_dir: Path | None = None, ui_dir: Path | None = None,
                 derivation: list[dict] | None = None):
        self.manifest = manifest
        self.run = run
        self.specimen = specimen
        self.scenario_dir = scenario_dir
        self.ui_dir = ui_dir or (Path(__file__).resolve().parents[2] / "ui")
        self.derivation = derivation or []
        self.lock = threading.RLock()
        self.mode = "live" if run is not None else "specimen"
        self._frame_cache: dict[tuple[str, int], tuple[float, Any]] = {}
        self.closed = False

    # -- read ------------------------------------------------------------
    def manifest_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "manifest": self.manifest.to_dict(),
            "human": projection.human(self.manifest, self._values(), sealed=self._sealed()),
            "agent": projection.agent(self.manifest),
            "summary": projection.summary(self.manifest),
            "diff": projection.diff(self.manifest),
            "derivation": self.derivation,
            "scenarios": self._scenarios(),
            "channels": {"events": "/api/events", "frame": "/api/frame"},
        }

    def state_payload(self) -> dict[str, Any]:
        with self.lock:
            if self.run is not None:
                return self.run.snapshot()
            spec = self.specimen or {}
            m = self.manifest
            return {
                "run_id": Path(spec.get("path", "specimen")).name,
                "status": "specimen",
                "controls": projection.human(m, spec.get("state", {}), sealed=True,
                                              readonly=True),
                "series": spec.get("series", {}),
                "steps": max((len(v) for v in spec.get("series", {}).values()), default=0),
                "interventions": spec.get("interventions", []),
                "records": [r.to_dict() for r in spec.get("records", [])][-200:],
                "summary": projection.summary(m),
            }

    def _values(self) -> dict[str, Any]:
        if self.run is not None:
            return self.run.state.values
        return (self.specimen or {}).get("state", {})

    def _sealed(self) -> bool:
        if self.run is None:
            return True
        return self.run.state.status not in ("created", "sealed")

    def _scenarios(self) -> list[dict[str, Any]]:
        if not self.scenario_dir or not self.scenario_dir.exists():
            return []
        from .scenario import Scenario

        out = []
        for p in sorted(self.scenario_dir.glob("*.toml")):
            try:
                sc = Scenario.load(p)
            except Exception:
                continue
            out.append({
                "name": sc.name,
                "file": p.name,
                "steps": [{"id": s.id, "kind": s.kind, "point": s.point, "op": s.op,
                           "value": s.value, "expect": s.expect, "note": s.text,
                           "timeout": s.timeout} for s in sc.steps],
            })
        return out

    # -- write -----------------------------------------------------------
    # -- frames ----------------------------------------------------------
    def frame(self, point: str, width: int = 480) -> "Frame":
        """Render one grid observation to a PNG preview.

        The full array never leaves the target: we fetch the producer's file once, decimate,
        colourise, and hand back a few kilobytes. That is the whole point of SPEC 4.6.
        """
        from .frames import make_frame, make_frame_from_png

        try:
            obs = self.manifest.observe(point)
        except SeamError:
            raise SeamError(f"no observation named {point!r}")
        if obs.shape != "grid":
            raise SeamError(f"{point}: shape is {obs.shape!r}, not grid")
        if not obs.source.startswith("file:"):
            raise SeamError(f"{point}: frame rendering needs a file: source, got {obs.source!r}")

        rel = obs.source.split(":", 1)[1]
        with self.lock:
            now = time.time()
            cached = self._frame_cache.get((point, width))
            if cached and now - cached[0] < (0.35 if self.run is not None else 86400):
                return cached[1]

            blob = self._read_artifact(rel)
            if blob is None:
                raise SeamError(
                    f"{point}: {rel} not produced yet. On Meep, fire `dump_slice` or "
                    "`stream_frames` first — a preview has to be produced before it can cross."
                )
            frame = make_frame_from_png(blob) or make_frame(
                blob, max_width=width,
                diverging=(obs.preview or {}).get("diverging"))
            self._frame_cache[(point, width)] = (now, frame)
            return frame

    def _read_artifact(self, rel: str) -> bytes | None:
        if self.run is not None:
            return self.run.ctx.transport.get(self.run.ctx.path(rel))
        spec = self.specimen or {}
        base = Path(spec.get("path", "."))
        p = base / "artifacts" / Path(rel).name
        return p.read_bytes() if p.exists() else None

    def intervene(self, point: str, value: Any) -> dict[str, Any]:
        with self.lock:
            if self.run is None:
                return {"point": point, "outcome": "rejected",
                        "note": "specimen mode: a specimen accepts no writes (modes/specimen.md)"}
            return self.run.apply(point, value, actor="human")

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.run is None:
                return {"outcome": "rejected", "note": "specimen mode"}
            return self.run.stop()

    def bench(self, point: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Re-derive one point under a proposed edit and report what changes.

        This is the bench's whole interaction: edit here, watch the *other projection* move.
        """
        import copy

        draft = copy.deepcopy(self.manifest)
        try:
            p = draft.param(point)
        except SeamError:
            return {"ok": False, "error": f"no point named {point}"}
        for k, v in patch.items():
            if k == "domain" and p.domain is not None:
                for dk, dv in v.items():
                    setattr(p.domain, dk, dv)
            else:
                setattr(p, k, v)
        try:
            draft.validate()
        except SeamError as e:
            return {"ok": False, "error": str(e)}
        d = projection.diff(draft)
        return {"ok": True, "diff": d, "agent": projection.agent(draft),
                "human": projection.human(draft, self._values(), sealed=self._sealed(),
                                          readonly=self.run is None)}


def make_handler(surface: Surface):
    class Handler(BaseHTTPRequestHandler):
        server_version = "seam/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                p = surface.ui_dir / "index.html"
                if not p.exists():
                    self._send(500, b"ui/index.html missing", "text/plain")
                    return
                self._send(200, p.read_bytes(), "text/html; charset=utf-8")
                return
            if u.path == "/api/manifest":
                self._json(surface.manifest_payload())
                return
            if u.path == "/api/state":
                self._json(surface.state_payload())
                return
            if u.path == "/api/events":
                self._stream()
                return
            if u.path == "/api/frame":
                qs = parse_qs(u.query)
                point = (qs.get("point") or [""])[0]
                try:
                    width = int((qs.get("w") or ["480"])[0])
                except ValueError:
                    width = 480
                try:
                    fr = surface.frame(point, width)
                except SeamError as e:
                    self._json({"error": str(e)}, 409)
                    return
                fmt = (qs.get("format") or ["png"])[0].lower()
                if fmt == "rgba" and fr.rgb:
                    # Raw RGB for a native binding: no decoder needed on the client, and the
                    # bytes can go straight into a GPU texture.
                    body = fr.rgb
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Seam-Width", str(fr.width))
                    self.send_header("X-Seam-Height", str(fr.height))
                    self.send_header("X-Seam-Frame", json.dumps(fr.as_dict()))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = fr.png
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Seam-Frame", json.dumps(fr.as_dict()))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/scenarios":
                self._json(surface._scenarios())
                return
            if u.path == "/api/scenario":
                name = parse_qs(u.query).get("name", [""])[0]
                for s in surface._scenarios():
                    if s["name"] == name or s["file"] == name:
                        self._json(s)
                        return
                self._json({"error": "not found"}, 404)
                return
            self._json({"error": "not found"}, 404)

        def _stream(self) -> None:
            """Server-sent events. One frame per change, a keepalive otherwise."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last = None
            try:
                self.wfile.write(b": seam surface connected\n\n")
                self.wfile.flush()
                while not surface.closed:
                    payload = surface.state_payload()
                    records = payload.get("records") or []
                    key = (payload.get("status"), records[-1]["seq"] if records else 0,
                           payload.get("steps"))
                    if key != last:
                        body = json.dumps(payload, ensure_ascii=False)
                        self.wfile.write(b"event: state\ndata: " + body.encode() + b"\n\n")
                        self.wfile.flush()
                        last = key
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return
            u = urlparse(self.path)
            if u.path == "/api/intervene":
                self._json(surface.intervene(body.get("point", ""), body.get("value")))
                return
            if u.path == "/api/stop":
                self._json(surface.stop())
                return
            if u.path == "/api/bench":
                self._json(surface.bench(body.get("point", ""), body.get("patch", {})))
                return
            self._json({"error": "not found"}, 404)

    return Handler


def serve(surface: Surface, port: int, *, block: bool = True) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(surface))
    if block:
        print(f"seam surface on http://127.0.0.1:{port}  ({surface.mode} mode)")
        print(f"  events  http://127.0.0.1:{port}/api/events")
        print(f"  frames  http://127.0.0.1:{port}/api/frame?point=<grid OP>&w=480")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            surface.closed = True
            httpd.server_close()
    else:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
    return httpd


def drive(run, *, on_finish=None) -> threading.Thread:
    """Tick the crossing loop in the background so the HTTP surface stays responsive."""

    def loop():
        try:
            while run.state.status in ("running", "sealed"):
                if not run.tick():
                    break
                time.sleep(run.poll_seconds)
        finally:
            if on_finish:
                on_finish(run)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
