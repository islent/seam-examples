"""seam — a reference implementation of the Seam Specification v0.1.

The package is deliberately small and dependency-free (stdlib only) so that the
specification's claims are checkable by reading a few hundred lines rather than by
trusting a framework.

Public entry points:

    from seam import manifest, affordance, probe, runtime, projection, scenario, specimen

Command line:

    seam derive   — probe a code and emit a manifest plus its derivation trace
    seam check    — validate a manifest and diff its two projections
    seam run      — seal, launch, trace, and optionally serve a surface
    seam scenario — run an executable tutorial against a job or a specimen
    seam specimen — capture, replay, and diff recorded runs
"""

from .model import (
    AFFORDANCES,
    GRADES,
    LIVENESS,
    SHAPES,
    CrossingRecord,
    Domain,
    Manifest,
    Observe,
    Param,
    Provenance,
    SeamError,
)

__version__ = "0.1.0"

__all__ = [
    "AFFORDANCES", "GRADES", "LIVENESS", "SHAPES",
    "CrossingRecord", "Domain", "Manifest", "Observe", "Param", "Provenance", "SeamError",
    "__version__",
]
