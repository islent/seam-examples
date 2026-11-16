"""Bridge: seam HTTP surface → a TOML snapshot a non-Python binding can read.

Why a file, in a project that already has SSE and HTTP?

Because the Makie binding is Julia, and a Julia binding should not need HTTP.jl and a JSON package
to read a manifest. SPEC §2(b) says transport is out of scope; this is the cheapest possible
demonstration of that claim — the seam is the *declaration*, and a well-specified one can be
carried by a file just as happily as by a socket.

TOML rather than JSON for one reason: it is in Julia's standard library. A binding that has to
install a parser before it can show a slider is a binding that will not be written.

The bridge is bidirectional: it writes `state.toml` and `frame.bin`, and it watches `cmd.toml`
for one pending intervention, which it POSTs and then deletes. One pending command is enough —
§4.7c says one gesture is one submission, so there is never more than one in flight.

    python experiments/makie/bridge.py --url http://127.0.0.1:8770 --out experiments/makie/live
"""

from __future__ import annotations

import argparse
import time
import urllib.request
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from seam.client import SeamClient  # noqa: E402

SERIES_KEEP = 240


# ---------------------------------------------------------------------------
# a very small TOML writer, ordered, for one known shape
# ---------------------------------------------------------------------------


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    if v is None:
        return '""'
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _inline(d: dict) -> str:
    def one(v):
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(_scalar(x) for x in v) + "]"
        return _scalar(v)

    return "{" + ", ".join(f"{k} = {one(v)}" for k, v in d.items() if v is not None) + "}"


def write_state(path: Path, man: dict, state: dict, group_order: list[str],
                frame_point: str = "") -> None:
    ident = (man.get("manifest") or {}).get("identity", {})
    lines: list[str] = []
    lines.append("# seam state snapshot — written by experiments/makie/bridge.py")
    lines.append("# Order is carried explicitly: a TOML parser need not preserve it.")
    lines.append("")
    lines.append("[seam]")
    lines.append(f'version = "0.1"')
    lines.append(f"name = {_scalar(ident.get('name', 'seam'))}")
    lines.append(f"code = {_scalar(ident.get('code', ''))}")
    lines.append(f"mode = {_scalar(man.get('mode', ''))}")
    lines.append(f"profile = {_scalar(man.get('profile') or '')}")
    lines.append(f"status = {_scalar(state.get('status', ''))}")
    lines.append(f"steps = {_scalar(state.get('steps', 0))}")
    lines.append(f"at = {_scalar(time.strftime('%Y-%m-%dT%H:%M:%S'))}")
    lines.append("groups = [" + ", ".join(_scalar(g) for g in group_order) + "]")
    lines.append(f"frame_point = {_scalar(frame_point)}")
    lines.append("")

    controls = state.get("controls") or []
    for c in controls:
        if c.get("role") == "intervention":
            lines.append("[[point]]")
            lines.append(f"name = {_scalar(c['name'])}")
            lines.append(f"affordance = {_scalar(c.get('affordance'))}")
            lines.append(f"liveness = {_scalar(c.get('liveness') or '')}")
            lines.append(f"grade = {_scalar(c.get('grade') or '')}")
            lines.append(f"group = {_scalar(c.get('group') or '')}")
            lines.append(f"unit = {_scalar(c.get('unit') or '')}")
            lines.append(f"doc = {_scalar(c.get('doc') or '')}")
            lines.append(f"disabled = {_scalar(bool(c.get('disabled')))}")
            lines.append(f"confirm = {_scalar(bool(c.get('confirm')))}")
            lines.append(f"precision = {int(c.get('precision') if c.get('precision') is not None else 4)}")
            lines.append(f"value = {_scalar(c.get('value'))}")
            dom = c.get("domain") or {}
            if dom:
                lines.append("domain = " + _inline({
                    k: dom.get(k) for k in ("kind", "lo", "hi", "step", "log", "dtype", "values")
                    if dom.get(k) is not None
                }))
            lines.append("")

    for o in controls:
        if o.get("role") != "observation":
            continue
        series = (state.get("series") or {}).get(o["name"]) or []
        nums = [x for x in series[-SERIES_KEEP:] if isinstance(x, (int, float))]
        lines.append("[[observe]]")
        lines.append(f"name = {_scalar(o['name'])}")
        lines.append(f"shape = {_scalar(o.get('shape'))}")
        lines.append(f"pitch = {_scalar(o.get('pitch'))}")
        lines.append(f"group = {_scalar(o.get('group') or '')}")
        lines.append(f"unit = {_scalar(o.get('unit') or '')}")
        lines.append(f"precision = {int(o.get('precision') if o.get('precision') is not None else 4)}")
        lines.append(f"value = {_scalar(o.get('value'))}")
        lines.append("series = [" + ", ".join(_scalar(float(x)) for x in nums) + "]")
        lines.append("")

    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_frame(path: Path, frame) -> str | None:
    """Header line, then raw RGB. No decoder needed on the reading side."""
    if frame is None:
        return None
    body = f"{frame.width} {frame.height} {frame.meta.get('lo', 0)} {frame.meta.get('hi', 0)}\n".encode()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body + frame.rgb)
    tmp.replace(path)
    return f"{frame.width}x{frame.height}"


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="seam → TOML bridge for the Makie binding")
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--out", default=str(ROOT / "experiments" / "makie" / "live"))
    ap.add_argument("--interval", type=float, default=0.8)
    ap.add_argument("--once", action="store_true", help="write one snapshot and exit")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.toml"
    frame_path = out / "frame.bin"
    cmd_path = out / "cmd.toml"

    client = SeamClient(args.url)
    man = {}
    last_cmd_mtime = 0.0
    print(f"bridge {args.url} -> {out}")

    while True:
        try:
            if not man:
                man = client.manifest()
            state = client.state()
        except Exception as e:  # noqa: BLE001
            state_path.write_text(f'[seam]\nstatus = "unreachable"\nname = "{e}"\n', encoding="utf-8")
            if args.once:
                return 1
            time.sleep(1.5)
            continue

        from seam.client import group_points

        controls = state.get("controls") or []
        order = [g for g, _ in group_points([c for c in controls if c.get("role") == "intervention"])]

        grids = [o for o in controls if o.get("role") == "observation" and o.get("shape") == "grid"]
        order_g = sorted(grids, key=lambda o: o.get("pitch") == "on-demand")
        frame = None
        frame_point = ""
        for o in order_g:
            frame = client.frame(o["name"], width=(o.get("preview") or {}).get("max_width", 120))
            if frame is not None:
                frame_point = o["name"]
                break
        size = write_frame(frame_path, frame)
        write_state(state_path, man, state, order, frame_point=frame_point)

        # one pending command, written by the Julia side
        if cmd_path.exists():
            mtime = cmd_path.stat().st_mtime
            if mtime > last_cmd_mtime:
                last_cmd_mtime = mtime
                try:
                    import tomllib

                    cmd = tomllib.loads(cmd_path.read_text(encoding="utf-8"))
                    point, value = cmd.get("point"), cmd.get("value")
                    if point:
                        r = client.intervene(point, value)
                        print(f"  {point} = {value!r} -> {r.get('outcome')}")
                except Exception as e:  # noqa: BLE001
                    print(f"  command failed: {e}")
                finally:
                    cmd_path.unlink(missing_ok=True)

        if args.once:
            print(f"wrote {state_path} frame={size}")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
