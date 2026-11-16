"""Evidence extraction — the executable form of derivation/PROBES.md.

Every harvester here returns ``RawPoint`` objects annotated with the evidence they used.
None of them assigns an affordance; that is ``affordance.decide``'s job and nobody else's.

Two deliberate restrictions:

* No harvester calls ``eval``. Deck values and paramfile values are parsed for *shape*, never
  evaluated. A deck value is a program in a namespace we do not control.
* No harvester invents bounds. If a range is not in the source, ``lo``/``hi`` stay ``None``
  and the point derives to ``unresolved`` or gets an explicit author bound at E0.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .affordance import Evidence, RawPoint

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_UNIT_SYMBOLS = {
    "femto": "fs", "femtosecond": "fs", "fs": "fs",
    "pico": "ps", "picosecond": "ps", "ps": "ps",
    "nano": "ns", "nanosecond": "ns", "ns": "ns",
    "micro": "um", "micron": "um", "micrometer": "um", "um": "um",
    "milli": "mm", "millimeter": "mm", "mm": "mm",
    "centi": "cm", "centimeter": "cm", "cm": "cm",
    "meter": "m", "m": "m",
    "kpc": "kpc", "Mpc": "Mpc", "pc": "pc",
    "Gyr": "Gyr", "Myr": "Myr", "yr": "yr",
    "Msun": "Msun", "K": "K", "eV": "eV", "keV": "keV", "MeV": "MeV",
}

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_EXPR_UNIT = re.compile(rf"^\s*({_NUM})\s*\*\s*([A-Za-z_]\w*)\s*$")
_PLAIN_NUM = re.compile(rf"^\s*({_NUM})\s*$")


def classify_token(value: str) -> tuple[str, Any, str | None]:
    """(dtype, literal_or_None, unit). Shape only — never evaluates."""
    v = value.strip()
    if m := _EXPR_UNIT.match(v):
        num, sym = float(m.group(1)), m.group(2)
        return ("int" if num.is_integer() else "float"), num, _UNIT_SYMBOLS.get(sym, sym)
    if m := _PLAIN_NUM.match(v):
        num = float(m.group(1))
        return ("int" if num.is_integer() and "." not in m.group(1) else "float"), num, None
    if v.lower() in ("true", "false", "yes", "no", "on", "off", "t", "f"):
        return "bool", v.lower() in ("true", "yes", "on", "t"), None
    if re.fullmatch(r"[A-Za-z_]\w*", v):
        return "str", v, None
    if re.search(r"[-+*/()]", v):
        # an expression over other parameters or deck functions: no closed form
        return "expression", None, None
    return "str", v, None


def _is_fn_expr(v: str) -> bool:
    return bool(re.match(r"^\s*[A-Za-z_]\w*\s*\(", v.strip()))


# --------------------------------------------------------------------------
# 1. EPOCH-style deck  (PROBES 3)
# --------------------------------------------------------------------------

_BLOCK = re.compile(r"^\s*(begin|end):(\w+)\s*$")
_KV = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_COMMENT = re.compile(r"^\s*#\s*(.*)$")


def harvest_deck(text: str, path: str, *, read_once: bool = True) -> list[RawPoint]:
    """Harvest a ``begin:<block>`` deck.

    Names are block-qualified (``control.t_end`` vs ``laser.t_end``) because a deck's
    parameter namespace is per-block and a flat namespace moves the wrong parameter.
    """
    points: dict[str, RawPoint] = {}
    block: str | None = None
    pending_comment: str | None = None

    for lineno, line in enumerate(text.splitlines(), 1):
        if m := _COMMENT.match(line):
            pending_comment = m.group(1).strip() or None
            continue
        if m := _BLOCK.match(line):
            block = m.group(2) if m.group(1) == "begin" else None
            continue
        if not block:
            continue
        m = _KV.match(line)
        if not m:
            if line.strip():
                pending_comment = None
            continue

        key, raw = m.group(1), m.group(2)
        qualified = f"{block}.{key}"
        dtype, lit, unit = classify_token(raw)

        if _is_fn_expr(raw) or dtype == "expression":
            # A waveform or a cross-reference. No finite domain; say so.
            p = RawPoint(name=qualified, writable=True, dtype="expression",
                         doc=pending_comment, unit=None)
            p.add("E1", "deck-grammar", f"{path}:{lineno}", detail=raw)
            points[qualified] = p
            pending_comment = None
            continue

        p = RawPoint(
            name=qualified,
            writable=True,
            dtype=dtype,
            default=lit,
            unit=unit,
            doc=pending_comment,
            adapter_key=f"{block}.{key}",
        )
        p.add("E1", "deck-grammar", f"{path}:{lineno}", detail=raw)
        if pending_comment:
            p.add("E2", "docstring", f"{path}:{lineno - 1}")
        if read_once:
            p.liveness = "sealed"
            p.liveness_evidence = Evidence(
                "E1", "architecture", f"{path}:deck",
                "deck is parsed once at start-up; no channel re-reads it",
            )
        points[qualified] = p
        pending_comment = None

    return list(points.values())


def deck_enumerations(text: str) -> dict[str, list[str]]:
    """Symbolic values seen for string-typed deck keys, to corroborate E2 enums."""
    seen: dict[str, set[str]] = {}
    block = None
    for line in text.splitlines():
        if m := _BLOCK.match(line):
            block = m.group(2) if m.group(1) == "begin" else None
            continue
        if not block:
            continue
        m = _KV.match(line)
        if not m:
            continue
        dtype, _, _ = classify_token(m.group(2))
        if dtype == "str":
            seen.setdefault(f"{block}.{m.group(1)}", set()).add(m.group(2).strip())
    return {k: sorted(v) for k, v in seen.items()}


# --------------------------------------------------------------------------
# 2. Gadget-2-style tag-value paramfile  (PROBES 4)
# --------------------------------------------------------------------------

_SScanf = re.compile(r'sscanf\s*\(\s*buf\s*,\s*"([^"]+)"')
_HANDLER = re.compile(r'(?:atoi|atof|strcpy)\s*\(\s*\w+\s*,\s*buf2\s*\)')
_TAG_STRCMP = re.compile(r'strcmp\s*\(\s*buf1\s*,\s*"(\w+)"\s*\)')


def harvest_paramreader(reader_src: str, path: str) -> dict[str, Any]:
    """Read the *reader*, not the file. The grammar lives here, not in the parameters."""
    out: dict[str, Any] = {"path": path, "grammar": None, "tags": [], "handlers": {}}
    if m := _SScanf.search(reader_src):
        out["grammar"] = m.group(1)
    out["tags"] = _TAG_STRCMP.findall(reader_src)
    # dtype per tag: find the strcmp branch and the first handler inside it
    for tag in out["tags"]:
        branch = re.search(
            rf'strcmp\s*\(\s*buf1\s*,\s*"{tag}"\s*\)[^{{]*{{(.{{0,400}}?)}}',
            reader_src, re.S,
        )
        if branch:
            h = branch.group(1)
            if "atoi" in h:
                out["handlers"][tag] = "int"
            elif "atof" in h:
                out["handlers"][tag] = "float"
            elif "strcpy" in h:
                out["handlers"][tag] = "str"
    return out


def harvest_paramfile(
    text: str,
    path: str,
    *,
    handlers: dict[str, str] | None = None,
    grammar: str | None = None,
) -> list[RawPoint]:
    """Harvest a ``Tag<whitespace>Value`` paramfile.

    The grammar is passed in from ``harvest_paramreader`` rather than assumed: this format
    looks like ``key = value`` and is not, and a harvester that assumes the latter produces
    a manifest the code cannot consume.
    """
    handlers = handlers or {}
    points: list[RawPoint] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("%") or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if len(parts) < 2:
            continue
        tag, raw = parts[0], parts[1].strip()

        if raw.startswith("="):
            # The reader's sscanf would store "=" as the value. Flag it loudly.
            p = RawPoint(name=tag, writable=True, dtype="unknown")
            p.add("E1", "paramfile-grammar", f"{path}:{lineno}",
                  detail="value starts with '='; the reader's sscanf stores '=' literally")
            points.append(p)
            continue

        dtype = handlers.get(tag)
        if dtype is None:
            dtype, lit, unit = classify_token(raw)
        else:
            _, lit, unit = classify_token(raw)
            if dtype == "int" and isinstance(lit, float):
                lit = int(lit)

        p = RawPoint(name=tag, writable=True, dtype=dtype, default=lit, unit=unit)
        p.add("E1", "paramfile-grammar", f"{path}:{lineno}", detail=raw)
        p.liveness = "sealed"
        p.liveness_evidence = Evidence(
            "E1", "architecture", f"{path}:paramfile",
            "read_parameter_file() runs once before any integration",
        )
        points.append(p)
    return points


# --------------------------------------------------------------------------
# 3. Julia @kwdef struct  (PROBES 5)
# --------------------------------------------------------------------------

_KWDEF = re.compile(r"@kwdef\s+struct\s+(\w+)(?:\{([^}]*)\})?")
_JL_FIELD = re.compile(r"^\s+(\w+)\s*(?:::\s*([\w\.\{\}\[\],\s]+?))?\s*=\s*(.+?)\s*$")
_JL_TYPE = re.compile(r"^\s+(\w+)\s*::\s*([\w\.\{\}\[\],\s]+?)\s*$")


def harvest_julia_struct(text: str, path: str, struct: str) -> list[RawPoint]:
    m = _KWDEF.search(text)
    if not m:
        raise ValueError(f"{path}: no @kwdef struct found")
    start = text.index("\n", m.end())
    lines = text[start:].splitlines()
    fields: dict[str, RawPoint] = {}
    doc: str | None = None
    for i, line in enumerate(lines, 1):
        if line.strip() == "end":
            break
        stripped = line.strip()
        if stripped.startswith('"""') and stripped.endswith('"""') and len(stripped) > 6:
            doc = stripped.strip('"').strip()
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith('"""'):
            continue
        fm = _JL_FIELD.match(line)
        tm = _JL_TYPE.match(line)
        name = jtype = default = None
        if fm:
            name, jtype, default = fm.group(1), fm.group(2), fm.group(3)
        elif tm:
            name, jtype = tm.group(1), tm.group(2)
        if not name or name == "end":
            continue
        if name in fields:
            continue

        jt = (jtype or "").strip()
        dtype, lit, unit = _julia_dtype(jt, default)
        p = RawPoint(
            name=name, writable=True, dtype=dtype, default=_julia_literal(default),
            unit=unit, doc=doc, lo=None, hi=None,
        )
        p.add("E1", "struct", f"{path}:{struct}.{name}", detail=jt or None)
        p.liveness = "sealed"
        p.liveness_evidence = Evidence(
            "E1", "architecture", f"{path}:struct {struct}",
            "immutable struct: fields cannot be assigned after construction",
        )
        fields[name] = p
        doc = None
    return list(fields.values())


def _julia_dtype(jtype: str, default: str | None) -> tuple[str, Any, str | None]:
    jt = jtype.strip()
    if jt in ("Bool",) or (default or "").strip() in ("true", "false"):
        return "bool", (default or "").strip() == "true", None
    if jt in ("Int", "Int32", "Int64", "Integer"):
        return "int", None, None
    if jt in ("Float64", "Float32", "Real", "AbstractFloat"):
        # Unitful: 0.1u"Gyr"
        if default and (u := re.search(r'u"([^"]+)"', default)):
            return "float", None, u.group(1)
        return "float", None, None
    if jt.startswith("String") or jt == "Symbol":
        return "str", None, None
    if jt.startswith("Union"):
        return "unknown", None, None
    if jt.startswith("Vector") or jt.startswith("Array") or jt.startswith("Dict"):
        return "list", None, None
    if jt.endswith(")"):
        # a type constructor call, e.g. Vacuum() — a dispatch choice
        return "enum", None, None
    return ("unknown" if jt else "unknown"), None, None


def _julia_literal(default: str | None) -> Any:
    if default is None:
        return None
    s = default.strip()
    if m := re.fullmatch(r"([-+]?[\d.]+(?:e[-+]?\d+)?)\s*u\"([^\"]+)\"", s):
        return float(m.group(1))
    if m := re.fullmatch(r"([-+]?[\d.]+(?:e[-+]?\d+)?)", s):
        v = float(m.group(1))
        return int(v) if v.is_integer() else v
    if s in ("true", "false"):
        return s == "true"
    return s


# --------------------------------------------------------------------------
# 4. Python source / signature  (PROBES 1)
# --------------------------------------------------------------------------


def harvest_python_init(text: str, path: str, cls: str | None = None) -> list[RawPoint]:
    """Harvest a class's ``__init__`` keyword arguments from source.

    Also records whether each name is later assigned to an attribute inside ``__init__``,
    which is the E1 route to the read-once fact: an attribute set in the constructor and
    never re-read is a sealed parameter.
    """
    tree = ast.parse(text)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and (cls is None or node.name == cls):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    target = item
                    break
        if target:
            break
    if target is None:
        raise ValueError(f"{path}: no __init__ found" + (f" in class {cls}" if cls else ""))

    args = target.args
    defaults: list[Any] = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    names = [(a.arg, d) for a, d in zip(args.args, defaults) if a.arg != "self"]
    names += [(a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults)]

    annotations = {a.arg: a.annotation for a in list(args.args) + list(args.kwonlyargs)}
    stored = {
        n.attr
        for node in ast.walk(target)
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
    }

    out: list[RawPoint] = []
    for name, dflt in names:
        ann = annotations.get(name)
        dtype = _py_dtype(ann)
        p = RawPoint(
            name=name,
            writable=True,
            dtype=dtype,
            default=ast.literal_eval(dflt) if isinstance(dflt, ast.Constant) else None,
        )
        p.add("E1", "signature", f"{path}:{cls or ''}.__init__({name})", detail=dtype)
        p.liveness = "sealed"
        p.liveness_evidence = Evidence(
            "E1", "architecture", f"{path}:{cls or ''}.__init__",
            "value is consumed at construction"
            + (" and stored as an attribute, never re-read" if name in stored else ""),
        )
        out.append(p)
    return out


def _py_dtype(ann: ast.expr | None) -> str:
    if ann is None:
        return "unknown"
    s = ast.unparse(ann)
    if s in ("int",):
        return "int"
    if s in ("float",):
        return "float"
    if s in ("bool",):
        return "bool"
    if s in ("str",):
        return "str"
    if s.startswith("Literal"):
        return "enum"
    if s.startswith(("list", "List", "dict", "Dict", "tuple")):
        return "list"
    return "unknown"


def harvest_argparse(text: str, path: str) -> list[RawPoint]:
    """Harvest ``add_argument`` calls structurally. E1: better than parsing ``--help``."""
    tree = ast.parse(text)
    out: list[RawPoint] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if not flags:
            continue
        name = flags[0].lstrip("-").replace("-", "_")
        kw = {k.arg: k.value for k in node.keywords}

        typ = _argparse_type(kw.get("type"))
        choices = None
        if "choices" in kw:
            try:
                choices = list(ast.literal_eval(kw["choices"]))
            except Exception:
                choices = None
        default = None
        if "default" in kw:
            try:
                default = ast.literal_eval(kw["default"])
            except Exception:
                default = None
        help_text = None
        if "help" in kw and isinstance(kw["help"], ast.Constant):
            help_text = str(kw["help"].value)

        if kw.get("action") and isinstance(kw["action"], ast.Constant):
            if kw["action"].value in ("store_true", "store_false"):
                typ = "bool"
                if default is None:
                    default = kw["action"].value == "store_true"

        p = RawPoint(name=name, writable=True, dtype=typ, values=choices,
                     default=default, doc=help_text)
        p.add("E1", "argparse", f"{path}:{node.lineno}", detail=ast.unparse(node)[:200])
        p.liveness = "sealed"
        p.liveness_evidence = Evidence(
            "E1", "architecture", f"{path}:argparse",
            "CLI arguments are parsed once at process start",
        )
        out.append(p)
    return out


def _argparse_type(node: ast.expr | None) -> str:
    if node is None:
        return "str"
    s = ast.unparse(node)
    return {"int": "int", "float": "float", "str": "str", "bool": "bool"}.get(s, "str")


# --------------------------------------------------------------------------
# file-system entry point
# --------------------------------------------------------------------------


def harvest_file(path: str | Path, kind: str, **kw: Any) -> list[RawPoint]:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if kind == "deck":
        return harvest_deck(text, str(p), **kw)
    if kind == "paramfile":
        return harvest_paramfile(text, str(p), **kw)
    if kind == "julia":
        return harvest_julia_struct(text, str(p), kw.pop("struct"))
    if kind == "python":
        return harvest_python_init(text, str(p), kw.get("cls"))
    if kind == "argparse":
        return harvest_argparse(text, str(p))
    raise ValueError(f"unknown harvest kind {kind!r}")
