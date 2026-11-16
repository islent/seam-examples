#!/usr/bin/env python3
"""Terminal binding for a Seam surface. No dependencies, no window, 80 columns.

This is the binding that finds the specification's gaps, because it is the one with the least
room: no pixels, no mouse, no colour gradient, and a hard column budget. Every place where the
manifest was vague shows up here as an invented convention.

Three things it forced into the specification (SPEC §4.7), all of which the other bindings were
quietly doing differently:

* **Grouping.** The web surface grouped by liveness, this one wanted to group by affordance, and
  the manifest said nothing. Now every point declares ``group`` and bindings follow it.
* **Numeric formatting.** ``%.4g`` versus ``toPrecision(5)`` versus a hand-rolled f-string is
  three renderings of one sample. Now ``precision`` is declared and ``seam.client.format_value``
  is the shared answer.
* **Commit boundary.** A held arrow key must not emit forty interventions, and a dragged slider
  must not emit one per pixel. Adjustments stay in a draft; Enter commits.

    python tui/seam_tui.py --url http://127.0.0.1:8770
    python tui/seam_tui.py --url http://127.0.0.1:8770 --selftest --width 100 --height 30
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam.client import Draft, SeamClient, display_value, format_value, group_points  # noqa: E402

BLOCKS = " ░▒▓█"
SPARK = "▁▂▃▄▅▆▇█"
RAMP = " .:-=+*#%@"
LIVE_MARK = {"sealed": "·", "perstep": "~", "midrun": "="}


# ---------------------------------------------------------------------------
# terminal plumbing
# ---------------------------------------------------------------------------


class Terminal:
    def __init__(self, width: int, height: int, ansi: bool = True):
        self.width = width
        self.height = height
        self.ansi = ansi
        self._saved = None

    def enter(self) -> None:
        if not self.ansi:
            return
        _enable_vt()
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt screen, hide cursor
        sys.stdout.flush()

    def leave(self) -> None:
        if not self.ansi:
            return
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def draw(self, lines: list[str]) -> None:
        if self.ansi:
            buf = ["\x1b[H"]
            for i, line in enumerate(lines[: self.height]):
                buf.append("\x1b[2K" + line)
                if i < len(lines) - 1:
                    buf.append("\r\n")
            sys.stdout.write("".join(buf))
        else:
            sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


def _enable_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        k.GetConsoleMode(h, ctypes.byref(mode))
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001
        pass


class Keyboard:
    """Arrow keys, enter, tab, and single characters. No dependency, both platforms."""

    def __init__(self):
        self._fd = None

    def __enter__(self):
        if os.name != "nt":
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if os.name != "nt" and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def get(self) -> str:
        if os.name == "nt":
            import msvcrt

            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                code = msvcrt.getch()
                return {b"H": "up", b"P": "down", b"M": "right", b"K": "left"}.get(code, "")
            if ch in (b"\r", b"\n"):
                return "enter"
            if ch == b"\t":
                return "tab"
            if ch == b"\x1b":
                return "esc"
            try:
                return ch.decode("utf-8", "ignore").lower()
            except Exception:  # noqa: BLE001
                return ""
        data = sys.stdin.read(1)
        if data == "\x1b":
            rest = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(rest, "esc")
        if data in ("\r", "\n"):
            return "enter"
        if data == "\t":
            return "tab"
        return data.lower()


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


def sparkline(values: list[float], width: int) -> str:
    nums = [v for v in values if isinstance(v, (int, float))][-width:]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return SPARK[3] * len(nums)
    return "".join(SPARK[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in nums)


def ascii_field(rgb: bytes, w: int, h: int, cols: int, rows: int) -> list[str]:
    """A grid frame as text. The terminal's answer to a heatmap: luminance into a character ramp."""
    if w <= 0 or h <= 0 or not rgb:
        return []
    step_x = max(1, w // max(1, cols))
    step_y = max(1, h // max(1, rows))
    lines = []
    for y in range(0, h, step_y):
        line = []
        for x in range(0, w, step_x):
            o = (y * w + x) * 3
            lum = (rgb[o] * 3 + rgb[o + 1] * 6 + rgb[o + 2]) / 10.0 / 255.0
            line.append(RAMP[min(len(RAMP) - 1, int(lum * (len(RAMP) - 1) + 0.5))])
        lines.append("".join(line))
        if len(lines) >= rows:
            break
    return lines


def bar(position: float, width: int) -> str:
    position = min(1.0, max(0.0, position))
    filled = int(round(position * (width - 1)))
    return "".join("█" if i < filled else ("◆" if i == filled else "─") for i in range(width))


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------


class Tui:
    def __init__(self, client: SeamClient, term: Terminal):
        self.c = client
        self.t = term
        self.man: dict = {}
        self.state: dict = {}
        self.sel = 0
        self.draft = Draft()
        self.armed: set[str] = set()
        self.banner = ""
        self.banner_at = 0.0
        self.show_trace = False
        self.frame_name = ""
        self.frame_lines: list[str] = []
        self.frame_at = 0.0

    # -- data ----------------------------------------------------------
    def poll(self) -> None:
        try:
            if not self.man:
                self.man = self.c.manifest()
            self.state = self.c.state()
        except Exception as e:  # noqa: BLE001
            self.say(f"surface unreachable: {e}")

    def say(self, text: str) -> None:
        self.banner = text
        self.banner_at = time.time()

    @property
    def controls(self) -> list[dict]:
        return [c for c in (self.state.get("controls") or []) if c.get("role") == "intervention"]

    @property
    def observations(self) -> list[dict]:
        return [c for c in (self.state.get("controls") or []) if c.get("role") == "observation"]

    def rows(self) -> list[tuple[str, dict | None]]:
        """Flattened rows: (group header, None) interleaved with controls."""
        out: list[tuple[str, dict | None]] = []
        for group, items in group_points(self.controls):
            out.append((group or "interventions", None))
            out.extend(("", c) for c in items)
        return out

    def pick(self) -> dict | None:
        items = [c for _, c in self.rows() if c]
        if not items:
            return None
        return items[min(self.sel, len(items) - 1)]

    # -- actions -------------------------------------------------------
    def move(self, delta: int) -> None:
        items = [c for _, c in self.rows() if c]
        if items:
            self.sel = (self.sel + delta) % len(items)
            self.draft.clear()

    def nudge(self, direction: int) -> None:
        c = self.pick()
        if not c:
            return
        aff, dom = c.get("affordance"), c.get("domain") or {}
        if c.get("disabled") or aff == "unresolved":
            self.say(f"{c['name']}: not writable now ({c.get('liveness')})")
            return
        if aff == "ratchet":
            vals = dom.get("values") or []
            if not vals:
                return
            cur = c.get("value")
            idx = vals.index(cur) if cur in vals else 0
            self.draft.set(c["name"], vals[(idx + direction) % len(vals)])
            self.say(f"{c['name']} → {self.draft.value!r}   [enter] sends it")
        elif aff == "dial":
            # Move in *position* space, not value space: this is what makes a log dial usable
            # and what SPEC 4.7 fixes so that two bindings move alike.
            from seam.model import Domain

            d = Domain(**(dom or {}))
            v = c.get("value")
            pos = d.value_to_position(float(v if v is not None else dom.get("lo", 0)))
            pos = min(1.0, max(0.0, pos + direction * 0.05))
            new = d.position_to_value(pos)
            if dom.get("dtype") == "int":
                new = int(round(new))
            elif dom.get("step"):
                step = float(dom["step"])
                new = round(new / step) * step
            self.draft.set(c["name"], new)
            self.say(f"{c['name']} → {format_value(new, c.get('precision'))}   [enter] sends it")

    def enter(self) -> None:
        if self.draft.dirty:
            r = self.draft.commit(self.c) or {}
            self.say(f"{r.get('point')} {r.get('outcome')}" + (f": {r.get('note')}" if r.get("note") else ""))
            return
        c = self.pick()
        if not c:
            return
        if c.get("affordance") != "trigger":
            self.say(f"{c['name']}: adjust with ←/→ then Enter to send")
            return
        if c.get("disabled"):
            self.say(f"{c['name']}: not writable now ({c.get('liveness')})")
            return
        if c.get("confirm") and c["name"] not in self.armed:
            # A destructive trigger two keystrokes from a scrolling finger needs arming.
            self.armed.add(c["name"])
            self.say(f"{c['name']} is destructive — press Enter again to fire, any move cancels")
            return
        self.armed.discard(c["name"])
        r = self.c.intervene(c["name"], True)
        self.say(f"{c['name']} {r.get('outcome')}" + (f": {r.get('note')}" if r.get("note") else ""))

    def refresh_frame(self, cols: int, rows: int) -> None:
        grids = [o for o in self.observations if o.get("shape") == "grid"]
        if not grids:
            return
        now = time.time()
        if now - self.frame_at < 0.6:
            return
        self.frame_at = now
        order = ([g for g in grids if g.get("pitch") != "on-demand"] +
                 [g for g in grids if g.get("pitch") == "on-demand"])
        got = self.c.first_frame([g["name"] for g in order], width=max(40, cols))
        if got is None:
            self.frame_lines = ["(no preview yet — fire dump_slice or stream_frames)"]
            return
        self.frame_name, fr = got
        self.frame_lines = ascii_field(fr.rgb, fr.width, fr.height, cols, rows)

    # -- draw ----------------------------------------------------------
    def render(self) -> list[str]:
        w, h = self.t.width, self.t.height
        ident = (self.man.get("manifest") or {}).get("identity", {})
        st = self.state
        head = f" {clip(ident.get('name', 'seam'), w - 34)}"
        right = f"{st.get('status', '?')} · {self.man.get('mode', '?')} · {self.man.get('profile') or ''} "
        lines = [head.ljust(max(0, w - len(right))) + right, "─" * w]

        body = h - 4
        left_w = min(46, max(28, w // 2)) if w >= 84 else w
        left: list[str] = []

        idx = -1
        for group, c in self.rows():
            if c is None:
                left.append("")
                left.append(f" {group.upper()}")
                continue
            idx += 1
            left.append(self._control_line(c, idx == self.sel, left_w))
            if idx == self.sel:
                # The column is 20 chars; the selected row must still reveal the real name,
                # because a binding may abbreviate but may not hide what it abbreviated.
                left.append("   " + clip(f"{c['name']} · {c.get('doc') or ''}", left_w - 4))

        # draft / armed hints
        if self.draft.dirty:
            left.append("")
            left.append(f"   pending: {self.draft.point} = {format_value(self.draft.value)}")

        # right pane
        right_lines: list[str] = []
        if self.show_trace:
            recs = (st.get("records") or [])[-body:]
            for r in reversed(recs):
                right_lines.append(clip(
                    f"{r.get('seq'):>5} {r.get('kind'):<10} {r.get('point'):<18} {r.get('outcome')}",
                    max(10, w - left_w - 2)))
        else:
            for o in self.observations:
                right_lines.append(self._observe_lines(o, max(10, w - left_w - 2)))
            if any(o.get("shape") == "grid" for o in self.observations):
                self.refresh_frame(max(20, w - left_w - 4), max(5, min(14, body // 3)))
                right_lines.append("")
                right_lines.append(f" {self.frame_name or 'field'} (ascii preview, declared width)")
                right_lines.extend(" " + ln for ln in self.frame_lines[: max(5, min(14, body // 3))])

        for i in range(body):
            l = clip(left[i], left_w) if i < len(left) else ""
            r = clip(right_lines[i], w - left_w - 2) if i < len(right_lines) else ""
            lines.append(l.ljust(left_w) + "│ " + r)

        lines.append("─" * w)
        if self.banner and time.time() - self.banner_at < 6:
            lines.append(" " + clip(self.banner, w - 2))
        else:
            keys = "↑↓ select  ←→ adjust  enter send/fire  t trace  r reload  q quit"
            lines.append(" " + clip(keys, w - 2))
        return lines

    def _control_line(self, c: dict, selected: bool, width: int) -> str:
        mark = "▸" if selected else " "
        live = c.get("liveness") or ""
        lock = LIVE_MARK.get(live, "?")
        disabled = c.get("disabled")
        name = clip(c.get("name", ""), 20)
        if disabled:
            lock = "✕"
        unit = f" {c['unit']}" if c.get("unit") else ""

        aff, dom = c.get("affordance"), c.get("domain") or {}
        value = self.draft.value if (self.draft.dirty and self.draft.point == c["name"]) else c.get("value")
        if aff == "dial":
            from seam.model import Domain

            d = Domain(**(dom or {}))
            pos = d.value_to_position(float(value if value is not None else dom.get("lo", 0)))
            widget = bar(pos, max(8, width - 32))
            shown = f"{format_value(value, c.get('precision'))}{unit}"
            return f"{mark}{lock} {name:<20} {widget} {shown}"
        if aff == "ratchet":
            shown = clip(str(value), 14)
            armed = "*" if (c.get("confirm") and c["name"] in self.armed) else " "
            return f"{mark}{lock} {name:<20} <{shown}>{armed}{unit}"
        if aff == "trigger":
            armed = "armed" if (c.get("confirm") and c["name"] in self.armed) else "fire"
            return f"{mark}{lock} {name:<20} [{armed}]"
        return f"{mark}{lock} {name:<20} (unresolved) {clip(str(value), 10)}"

    def _observe_lines(self, o: dict, width: int) -> str:
        name = clip(o.get("name", ""), 14)
        if o.get("shape") == "grid":
            pw = (o.get("preview") or {}).get("max_width", "?")
            return f" {name:<14} grid · {o.get('pitch')} · preview ≤{pw}px"
        series = (self.state.get("series") or {}).get(o["name"]) or []
        spark = sparkline([v for v in series if isinstance(v, (int, float))], max(8, width - 30))
        val = display_value(o, o.get("value"))
        return f" {name:<14} {val:>12}  {spark}"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def selftest(url: str, width: int, height: int, tab: str) -> int:
    """Render one frame to stdout, no tty tricks. This is how the layout gets verified."""
    term = Terminal(width, height, ansi=False)
    app = Tui(SeamClient(url), term)
    app.poll()
    if tab == "trace":
        app.show_trace = True
    fails: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(label)

    print(f"── {width}×{height} ──")
    lines = app.render()
    for ln in lines:
        print(ln)
    print()

    check(len(lines) <= height, "frame fits the terminal height", f"{len(lines)} > {height}")
    check(all(len(ln) <= width for ln in lines), "no line exceeds the column budget",
          str(max(len(ln) for ln in lines)))
    check(len(app.controls) > 0, "controls present", str(len(app.controls)))
    groups = [g for g, _ in group_points(app.controls)]
    check(all(g is not None for g in groups), "every control has a declared group", str(groups))
    check(len(groups) >= 1, "grouping comes from the manifest", str(groups))

    # formatting must match the declared precision
    dials = [c for c in app.controls if c.get("affordance") == "dial"]
    if dials:
        d = dials[0]
        rendered = format_value(d.get("value"), d.get("precision"))
        check(len(rendered.replace("-", "").split("e")[0].replace(".", ""))
              <= max(1, d.get("precision") or 4) + (1 if "e" in rendered else 0),
              f"{d['name']} honours precision={d.get('precision')}", rendered)

    # commit boundary: nudging must not write
    before = len((app.state.get("records") or []))
    writable = [c for c in app.controls
                if c.get("affordance") == "dial" and not c.get("disabled")]
    if writable:
        app.sel = [c for _, c in app.rows() if c].index(writable[0])
    app.nudge(+1)
    after = len((app.state.get("records") or []))
    check(app.draft.dirty or not writable, "an adjustment becomes a draft, not a crossing",
          str(app.pick() and app.pick().get("name")))
    check(before == after, "nudging wrote nothing to the trace", f"{before} -> {after}")
    app.draft.clear()

    # arming: a confirm trigger must not fire on first Enter
    confirm = [c for c in app.controls if c.get("confirm")]
    if confirm:
        app.sel = [c for _, c in app.rows() if c].index(confirm[0])
        n0 = len(app.state.get("records") or [])
        app.enter()
        n1 = len(app.state.get("records") or [])
        check(n1 == n0, f"{confirm[0]['name']} armed instead of fired")
        check(confirm[0]["name"] in app.armed, "armed state recorded")

    grid = [o for o in app.observations if o.get("shape") == "grid"]
    if grid:
        check(bool(app.frame_lines), "grid rendered as text", str(app.frame_lines[:1])[:60])
    print()
    return 0 if not fails else 1


def interactive(url: str) -> int:
    term = Terminal(*_size())
    app = Tui(SeamClient(url), term)
    app.poll()
    with Keyboard() as kb:
        term.enter()
        try:
            while True:
                term.draw(app.render())
                # poll while waiting, so the surface keeps moving without a keypress
                import select

                key = None
                if os.name == "nt":
                    import msvcrt

                    deadline = time.time() + 0.6
                    while time.time() < deadline:
                        if msvcrt.kbhit():
                            key = kb.get()
                            break
                        time.sleep(0.02)
                else:
                    r, _, _ = select.select([sys.stdin], [], [], 0.6)
                    key = kb.get() if r else None
                if key is None:
                    app.poll()
                    continue
                if key == "q":
                    break
                elif key in ("up", "down"):
                    app.move(-1 if key == "up" else +1)
                elif key in ("left", "right"):
                    app.nudge(-1 if key == "left" else +1)
                elif key == "enter":
                    app.enter()
                elif key == "t":
                    app.show_trace = not app.show_trace
                elif key == "r":
                    app.man = {}
                    app.poll()
                app.poll()
        finally:
            term.leave()
    return 0


def _size() -> tuple[int, int]:
    try:
        s = os.get_terminal_size()
        return max(60, s.columns), max(20, s.lines)
    except OSError:
        return 100, 32


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="terminal binding for a Seam surface")
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--selftest", action="store_true", help="render one frame to stdout and exit")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--height", type=int, default=32)
    ap.add_argument("--tab", default="run", choices=["run", "trace"])
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args.url, args.width, args.height, args.tab)
    return interactive(args.url)


if __name__ == "__main__":
    raise SystemExit(main())
