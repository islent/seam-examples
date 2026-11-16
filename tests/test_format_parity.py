"""Format parity: the Python and JavaScript renderers must agree, digit for digit.

SPEC 4.7 makes `precision` normative, which only means something if two implementations produce
the same string. `seam.client.format_value` and the `fmtValue` in `ui/index.html` are independent
implementations of one rule, so they get compared on a matrix of awkward values.

This caught a real defect: both stripped trailing zeros from a scientific mantissa, so a declared
precision of 3 rendered as `4e-10` instead of `4.00e-10`. They agreed with each other and were both
wrong — a consistency check is not a correctness check, which is why this file also asserts the
*expected* strings and not only equality.

    python tests/test_format_parity.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seam.client import format_value  # noqa: E402

CASES: list[tuple[float | int, int | None]] = [
    (3.9999e-10, 3), (3.571e-10, 3), (1.0e-10, 2), (-5.5e-12, 3),
    (304.0, 4), (900, 3), (40, 0), (0.5, 3), (0.5, 1),
    (1234.567, 4), (0.000123, 3), (-4228.76, 5), (0.0, 4), (2, 3),
    (1.234567e6, 4), (9.99e-3, 3), (-0.0, 2), (1e-300, 3), (6.02214076e23, 4),
]

# Strings the rule *must* produce, independent of any implementation.
EXPECT = {
    (3.9999e-10, 3): "4.00e-10",   # trailing zeros are precision, not noise
    (3.571e-10, 3): "3.57e-10",
    (304.0, 4): "304",
    (900, 3): "900",
    (40, 0): "40",
    (0.5, 3): "0.5",
    (0.5, 1): "0.5",
    (0.000123, 3): "1.23e-4",
    (-4228.76, 5): "-4228.8",
    (2, 3): "2",
}


def js_formatter() -> str:
    src = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"function fmtValue\(v, precision\) \{.*?\n\}", src, re.S)
    if not m:
        raise SystemExit("fmtValue not found in ui/index.html")
    return "const DEFAULT_PRECISION = 4;\n" + m.group(0)


def run_node(cases: list[tuple]) -> list[str]:
    script = js_formatter() + (
        "\nconst CASES = " + json.dumps([[v, p] for v, p in cases]) + ";\n"
        "console.log(JSON.stringify(CASES.map(([v, p]) => fmtValue(v, p))));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise SystemExit("node failed: " + r.stderr[-400:])
        return json.loads(r.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


def julia_binary() -> str | None:
    import shutil

    for cand in ("julia", "julia.exe",
                 r"D:/Programs/julia-1.13.3/bin/julia.exe",
                 r"C:/Programs/julia/bin/julia.exe"):
        found = shutil.which(cand) if "/" not in cand and "\\" not in cand else (
            cand if Path(cand).exists() else None)
        if found:
            return found
    return None


def run_julia(cases: list[tuple]) -> list[str] | None:
    """Third implementation. SeamFormat.jl is standalone so this needs no Makie load."""
    julia = julia_binary()
    if julia is None:
        return None
    pairs = ",\n  ".join(f"({v!r}, {p})" for v, p in cases)
    script = (
        f'include(raw"{ (ROOT / "experiments" / "makie" / "SeamFormat.jl").as_posix() }")\n'
        "using .SeamFormat\n"
        f"cases = [\n  {pairs}\n]\n"
        "for (v, p) in cases\n"
        "    println(seam_format(v, p))\n"
        "end\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".jl", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        r = subprocess.run([julia, "--startup-file=no", path], capture_output=True,
                           text=True, timeout=180)
        if r.returncode != 0:
            print("  ..   julia failed:", r.stderr.strip().splitlines()[-1] if r.stderr else "?")
            return None
        return [ln for ln in r.stdout.splitlines() if ln != ""]
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> int:
    fails: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            fails.append(label)

    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        print("node not available; format parity untested")
        return 0

    py = [format_value(v, p) for v, p in CASES]
    js = run_node(CASES)

    mismatched = [(v, p, a, b) for (v, p), a, b in zip(CASES, py, js) if a != b]
    check(not mismatched, "python and javascript agree on every case",
          "; ".join(f"{v}@{p}: py={a} js={b}" for v, p, a, b in mismatched[:4]))

    wrong = []
    for (v, p), expected in EXPECT.items():
        got = format_value(v, p)
        if got != expected:
            wrong.append(f"{v}@{p}: {got} != {expected}")
    check(not wrong, "the rule produces the intended strings, not merely consistent ones",
          "; ".join(wrong[:4]))

    if julia_binary() is None:
        print("  ..   julia not found; the third implementation is untested")
    else:
        jl = run_julia(CASES)
        if jl is None:
            check(False, "julia runs the formatting rule")
        else:
            bad = [(v, p, a, b) for (v, p), a, b in zip(CASES, py, jl) if a != b]
            check(not bad, "julia agrees with python and javascript",
                  "; ".join(f"{v}@{p}: py={a} jl={b}" for v, p, a, b in bad[:4]))

    trailing = format_value(3.9999e-10, 3)
    check(len(trailing.split("e")[0].replace(".", "")) == 3,
          "scientific mantissa keeps the declared digits", trailing)
    check(format_value(0.5, 3) == "0.5", "fixed notation drops redundant trailing zeros", format_value(0.5, 3))

    print()
    if fails:
        print(f"{len(fails)} FAILURE(S)")
        return 1
    print("format parity ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
