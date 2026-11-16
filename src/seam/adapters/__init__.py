"""Adapters: one per exposure shape.

Each module registers itself with ``seam.adapter.register`` on import. Import is lazy
(``adapter._bootstrap``) so that a manifest for Meep can be validated on a machine that has
no Julia, and vice versa.
"""

from __future__ import annotations


def _seam_line(payload: dict) -> str:
    """The one shared log convention.

    Adapters that generate a script (meep, astronbodysim) print observations as
    ``SEAM <json>`` so that the runtime's parser is one regex and not five. This is a
    transport detail, not part of the specification.
    """
    import json

    return "SEAM " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_seam_line(line: str) -> dict | None:
    import json

    if not line.startswith("SEAM "):
        return None
    try:
        return json.loads(line[5:])
    except json.JSONDecodeError:
        return None
