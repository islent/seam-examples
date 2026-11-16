# seam-examples

**Reference implementations of the Seam Specification v0.1-draft — not stable, seeking
criticism.** Four codes, one shared HPC box, one manifest each.

<sub>The specification lives in [its own repository](https://github.com/islent/seam); this one only
instantiates it. If the two disagree, the specification wins. Chinese readers: the spec repository
carries a [中文 README](https://github.com/islent/seam/blob/main/README.zh-CN.md) that explains the
same ideas without the vocabulary.</sub>

The smallest set of implementations that tests whether the specification's abstractions hold:
four ways of exposing a parameter surface, three physics domains, two levels of conformance. You
install it to check those claims, nothing more.

| Code | Exposure mechanism | Profile | Writable / readable | Looks for |
| :--- | :--- | :--- | :--- | :--- |
| **Meep 1.33** | Python object constructor | **L2** | 10 / 6 | `$SEAM_MEEP_PYTHON`, else `/opt/conda/envs/mp/bin/python` |
| **EPOCH3D** | `begin:`/`end:` text deck | **L1** | 8 / 3 | `$SEAM_EPOCH_BIN`, else `/opt/epoch/epoch3d/bin/epoch3d` |
| **Gadget-2.0.7** | `Tag<whitespace>Value` paramfile | **L1** | 8 / 4 | `$SEAM_GADGET2_BIN`, else `/opt/gadget2/bin/` |
| **AstroNbodySim.jl** | Julia immutable struct + callbacks | **L2** | 8 / 3 | `$SEAM_JULIA`, `$SEAM_ANS_PROJECT` |

Everything below was run against one shared HPC node. Paths in the manifests are conventional
defaults; point the `SEAM_*` variables (or the manifest `entry`) at your own installs. The traces
and specimens in this repository are outputs of those runs.

---

## The one thing to look at first

```
$ seam derive --kind deck --path data/epoch_laser.deck --name epoch-probe --code EPOCH3D \
              --bound control.nx=16,512 --bound control.t_end=10,2000

harvested 14 points from data/epoch_laser.deck

point                  affordance  liveness  cls evidence        reason
control.nx             dial        sealed    E0  author
control.t_end          dial        sealed    E0  author
control.x_min          unresolved  -         E1  deck-grammar    continuous with no bounds anywhere: ...
control.x_max          unresolved  -         E1  deck-grammar    structured value (expression) does ...
boundaries.bc_x_min    unresolved  -         E1  deck-grammar    string with no enumeration: a text box ...
laser.t_profile        unresolved  -         E1  deck-grammar    structured value (expression) does ...
output.dt_snapshot     dial        sealed    E0  author
output.ey              unresolved  -         E1  deck-grammar    string with no enumeration: ...
```

Eleven of fourteen points stay **`unresolved`**, which is the right answer. The deck declares
`t_end = 50 * femto` — a float with a unit and no range anywhere in the source. Seam's job is to
say "I consulted the grammar, it contains no bound" and hand the question back to the author, who
answers it at **E0**:

```
--bound control.t_end=10,2000        # recorded as provenance E0 / author / witness cli:--bound
```

Two of the fourteen cannot be resolved by anyone, and are not errors:

- `x_max = -x_min` is an expression over another parameter. A dial over it would let the operator
  build an inconsistent box.
- `t_profile = gauss(time,4*femto,4*femto)` is a waveform. There is no closed form of dials for
  it, and inventing three Gaussian knobs the deck does not have would be fiction.

That output is the whole specification on one screen: four affordances, derived mechanically
from evidence, with refusal as a first-class answer.

---

## How a client reaches the backend

The first question a browser raises: **the page is not static.** `seam run --serve` is one
process doing two things — it holds the SSH transport to the cluster and drives the crossing
loop, and it serves the surface on localhost. The browser talks to `127.0.0.1`; the seam process
is the thing with the credentials, the job handle and the trace.

```
   cluster (hpc node)                your machine
   ┌──────────────┐   ssh, one      ┌────────────────────────────────┐
   │ Meep / EPOCH │   round trip    │  seam run --serve 8770         │
   │ Gadget-2     │◄── per tick ───►│   ├── crossing loop            │
   │ Julia        │                 │   ├── trace.jsonl  (local)     │
   └──────────────┘                 │   └── HTTP + SSE on 127.0.0.1   │
                                    └───────────┬────────────────────┘
                                     browser tab │ terminal
                                    (SSE + PNG)  │ (HTTP poll)
```

Two channels, both deliberate:

| Channel | Purpose | Why this one |
| :--- | :--- | :--- |
| `GET /api/events` (SSE) | push one `state` frame per crossing batch | the SSH round trip is already about a second; polling at 1.5 s adds 1.5 s of dead time, polling at 100 ms asks the same question fifteen times |
| `GET /api/frame?point=&w=&format=` | grids as `png` or raw `rgba` | a binding that has to decode a PNG before uploading a texture is doing work the server already did |

## Quickstart

```bash
pip install -e .           # stdlib only; no dependencies

seam check manifests/meep.toml --agent        # validate + diff both projections
seam run manifests/meep.toml --host "$SEAM_HOST" --serve 8770
seam specimen serve specimens/meep-first-pulse --port 8771
seam scenario scenarios/meep-first-pulse.toml --host "$SEAM_HOST"
seam specimen diff specimens/epoch-laser specimens/epoch-laser-fork

# post-processing is a run in its own right: give it the previous run as its input
seam run manifests/postproc-gadget.toml --host "$SEAM_HOST" \
    --input '$HOME/seam-runs/g2-long'
```

`seam run --serve` prints a URL. Four modes are served from the same manifest:

| Mode | What it is | What to do in it |
| :--- | :--- | :--- |
| **Surface** | the live seam: controls, gauges, trace tail | move a live `dial`, fire a `trigger`, watch a `sealed` control refuse |
| **Tutorial** | an executable scenario, stepped | apply each step; a failed `expect` stops the tutorial rather than lying |
| **Bench** | the authoring surface | edit a point's affordance or bounds, watch the *agent* projection change, see invariant violations |
| **Specimen** | a frozen run | click an intervention and land on the observation that caused it |

---

## Real-time field rendering

A `grid` observation at a live pitch must declare a **preview** (`max_width`, `diverging`), and
what crosses the seam is a reduced frame — never the array (SPEC §4.6). Meep is the one code here
that can produce one per step: a `stream_frames` trigger turns on a 1/4-size `|Ez|` dump inside
the step callback, and the surface polls `/api/frame` at its own cadence.

The pipeline is stdlib: `.npy` parse → box-average to `max_width` → colour → PNG. No numpy in the
core, because the reference implementation is the part a reader has to be able to audit.

**The finding that put §4.6 into the spec.** Rendered at native 160×160, a driven point source
looks like a lattice of fine structure that is not in the field:

| raw 160×160 Yee sample | after 2×2 box average |
| :--- | :--- |
| `data/meep-slice-frame.png` (checkerboard, sign flips every pixel) | `data/meep-frame-w80.png` (central lobe, four-fold near field, absorbed edge) |

Adjacent cells flip sign because the raw sample carries the grid-scale (Nyquist) mode of the Yee
lattice. It has zero group velocity and no physical meaning, and at native resolution it reads as
structure. Box-averaging cancels it exactly. **Downsampling is a filter** — which is why the
*producer* reduces, to a width the manifest declared, instead of each viewer improvising its own.

An adapter that cannot produce a preview declares the OP `on-demand` and says why. EPOCH's SDF
dumps and Gadget-2's HDF5 snapshots are declared exactly that way: reading them needs a format
reader, and a specification should not require one to be called conformant.

## Post-processing is a run

A "post-process" menu item has parameters that are undeclared, untraced and invisible to the
agent. A standalone script is declared but unmanaged — no manifest, no crossings, no record of
which numbers ran on which data.

`manifests/postproc-gadget.toml` makes it a **second seam**: same primitives, same seal, same
trace, same specimen. It happens to read another run's `energy.txt` instead of integrating a PDE,
and that difference is one line of the adapter.

```bash
seam run manifests/postproc-gadget.toml --host "$SEAM_HOST" \
    --input '$HOME/seam-runs/g2-long' --set window=3 --set tolerance=0.05
```

```
records 35  {'seal': 1, 'transition': 2, 'observe': 32}
virial_ratio  1.05 → 1.31 → 1.02 → 0.97 → 0.99
drift_smoothed 0.00 → 0.09 → 0.20 → 0.34 → 0.40
within_tolerance 0.0
```

That verdict is the interesting part: energy drift hits 34 % and `within_tolerance` returns
`0.0`, independently confirming what `seam/mappings/gadget2.md` says in prose — the test initial
condition is not a true Plummer equilibrium distribution function, and the first quarter of code
time is the cluster relaxing. A post-processing seam willing to disagree with the README is worth
more than one that only prints numbers.

## Two surfaces

Same manifest, two consumers, one rule: **a binding may change how things look, and may not
change what they admit or what they print.**

| Binding | Transport | Frames | Why it ships |
| :--- | :--- | :--- | :--- |
| Web (`ui/index.html`) | SSE + fetch | PNG `<img>` | nothing but a URL; one file, no build step, no framework |
| Terminal (`tui/seam_tui.py`) | HTTP poll | RGB → ASCII ramp | works over any SSH session, no display, no install — and it found every gap in §4.7 |

```bash
python tui/seam_tui.py --url http://127.0.0.1:8770
python tui/seam_tui.py --url http://127.0.0.1:8770 --selftest --width 100 --height 34
python tests/test_bindings.py     # both surfaces against one live Meep run
```

The terminal surface, rendered by `--selftest` (no tty, so this is checkable without eyes):

```
▸✕ resolution           ██◆─────────── 40 px/a    │  ez_max        5.29e-10  ▆█▃▆█▂▅▇▂▅▇
 ✕ Courant              ██████◆─────── 0.5        │  ez_rms        2.09e-10  ▅█▁▅█▁▅█▁▅█▁
 ✕ pml_thickness        ███◆────────── 1 a        │  ez_slice      grid · on-demand · preview ≤120px
 ✕ dimensions           <2>                       │  ez_frame      grid · per-step · preview ≤120px
 NUMERICS                                         │
 ✕ eps_averaging        <True>                    │  ez_frame (ascii preview, declared width)
 RUN CONTROL                                      │  @@%%%%%%#--------::::::---------#%%%%%@@
 ~ until                █████████████◆ 900 a/c    │  @%%%%%%#+=====--::....::--=====+#%%%%%@
 ~ stop                 [fire]                    │  @%%%%%%#****+=--:......:--=+****#%%%%%%
```

At 80 columns there is no room to hide an invented convention, so writing this one produced
SPEC §4.7 in full:

| Gap | What the bindings were doing | Now |
| :--- | :--- | :--- |
| **Grouping** | web grouped by liveness, terminal wanted by affordance, a third by neither | `group` is declared; order is manifest order, ungrouped last |
| **Formatting** | `%.4g` vs `toPrecision(5)` vs a hand-rolled f-string — three renderings of one sample | `precision` is declared; `seam.client.format_value` is the shared rule |
| **Commit boundary** | ImGui submitted once **per frame of a drag**; a held arrow key would submit per keypress | adjustments stay in a draft; one submission is one crossing |
| **Log dials** | the web slider was linear in value; nobody had specified otherwise | position→value mapping is normative (linear, or geometric when `log`) |
| **Arming** | `stop` sat one keypress from `dump_slice`; the agent projection had a `confirm` argument and the human one had nothing | `confirm` is declared, and a binding must arm before firing |

Two of those were bugs in *this* repository: the ImGui slider firing per frame, and the formatter
stripping trailing zeros so a declared precision of 3 printed `4e-10`. The second one is the
interesting failure — it did so **in both implementations**, so a cross-binding consistency check
would have passed it. `tests/test_format_parity.py` therefore asserts expected strings as well as
equality, and it does so in two languages plus a Julia reimplementation.

Four more bindings were written to answer narrower questions — ImGui (raw RGB textures), Electron
(a Chromium shell), Tk (retained mode), Makie (Julia, a third transport). They live in
[`experiments/`](experiments/) with their own notes. They are what produced the table above;
they are not what this repository asks you to install.

---

## What each implementation proved

### Meep — the code that can be steered

Meep is in-process Python, so the step callback is a real channel. The adapter writes a control
file that `mp.at_every` reads, and two writes are genuinely live:

- `until` — extended from 200 to 480 **mid-run**. The callback raises out of `run()`, the outer
  loop re-enters `run(until=480)`. This is a documented Meep pattern, so the liveness claim is
  E4-backed rather than hopeful.
- `dump_slice` — writes the current `Ez` array at the next callback.

`tests/test_live.py` also asserts the refusals:

```
ok   mid-run extend of `until` accepted
ok   write to sealed `resolution` rejected
ok   out-of-domain write rejected at the seam, not passed to the job
ok   mid-run `dump_slice` accepted
ok   the run passed t=200 after being extended
ok   every intervention links to a preceding observation
```

The third one is worth pausing on. The test wrote `until = 9000` against a declared domain of
`[5, 500]`. The seam rejected it *before it reached the job* and recorded the rejection. Without
that, the operator would believe the run had been extended for the next twenty minutes.

**Meep's trap, documented in `seam/mappings/meep.md`:** Python will let you assign
`sim.resolution = 60` mid-run. The assignment succeeds and the run is then wrong. **Writability
is not liveness**, and this is the code that proves it.

### EPOCH — the code that cannot

The deck is parsed once, at start-up. There is no channel, no callback, no re-read. Every
parameter is `sealed`, and the only `midrun` intervention is SIGTERM.

A surface that offered a live `nx` slider over EPOCH would be offering a control that does
nothing, and the operator would believe it.

Two facts about this build were only discoverable by running it (neither is in the manual):

1. It is **interactive**: it prints `Specify output directory` and reads a path from stdin, then
   looks for `<path>/input.deck`. Feeding the deck on stdin makes the first block header become
   the directory name, and the run dies with `Input deck file "begin:control/input.deck" does
   not exist`.
2. Domain bounds belong in `begin:control` (EPOCH 4 style), not `begin:boundaries`. Putting them
   in `boundaries` produces `Unrecognised element "x_min"` warnings and a later failure on
   species boundaries.

The live channel is `epoch3d.dat`, appended once per snapshot write. 301 snapshot writes were
observed as 301 separate crossing records — an append-only table is a series, not a state.

### Gadget-2 — the format that lies

The paramfile looks like `key = value` and is not:

```c
if(sscanf(buf, "%s%s%s", buf1, buf2, buf3) < 2) continue;
```

A line is `Tag<whitespace>Value`. Writing `Tag = Value` makes `=` the *value*; every later
`strcpy` inherits it and the run dies in a different directory with
`cp: cannot create regular file '=/parameters-usedvalues'`.

The adapter therefore rewrites the code's **own reference paramfile**, changing only the tags in
the manifest. A manifest that lists 8 parameters cannot satisfy a build that requires 20 — the
first attempt here failed with eighteen consecutive `Error. I miss a value for tag ...` lines.

The unit trap is declared rather than assumed:

```toml
[[param]]
name = "TimeMax"
unit = "Gyr"
display_scale = 0.9778      # Gyr per code time unit, from the paramfile's own unit block
```

`UnitLength/UnitVelocity = 3.0857e16 s = 0.9778 Gyr`, not 1 Gyr. A 2.2 % error: small enough to
survive review, large enough to invalidate a comparison between two codes.

### AstroNbodySim — the code that already had a seam

`poststep::Vector{Function}` is invoked after every step (`src/run.jl:154`). So `perstep` liveness
is **structural evidence at E1** — the signature declares the channel exists, and no probe is
needed to establish it. This is the cheapest legitimate `perstep` of the four, and it is what a
code author who wanted to be intervenable would build.

Everything in the constructor is `sealed`, and unlike Meep the language enforces it:
`struct Simulation{D}` is immutable, so a mid-run field assignment is not unwise, it is
impossible.

---

## Layout

```
seam-examples/
├── src/seam/
│   ├── model.py        dataclasses mirroring the schema, plus the §4.1 invariants
│   ├── manifest.py     TOML ⇄ JSON, lossless, order-preserving
│   ├── affordance.py   writeability × domain × liveness → affordance (the decision table)
│   ├── probe.py        E1 harvesters: deck grammar, paramfile reader, Julia struct, Python AST
│   ├── projection.py   the two projections, and the mechanical divergence check
│   ├── transport.py    local + ssh, byte-safe, one dependency fewer
│   ├── adapter.py      the four questions an adapter answers
│   ├── runtime.py      seal → launch → poll → observe → intervene → finish
│   ├── record.py       append-only trace with caused_by
│   ├── scenario.py     executable tutorials, live or replayed
│   ├── specimen.py     capture / open / diff
│   ├── client.py       the canonical client: one answer to formatting/grouping/commit
│   ├── frames.py       npy → decimate → colour → PNG (no numpy, no PIL)
│   ├── server.py       HTTP surface: REST + SSE + binary frames
│   └── cli.py          seam check | derive | agent | run | scenario | specimen
├── adapters/{meep,epoch,gadget2,astronbodysim}/   one module each
├── manifests/          four manifests + one post-processing seam
├── scenarios/          two executable tutorials
├── specimens/          four captured runs
├── ui/index.html       the web surface
├── tui/seam_tui.py     the terminal surface
├── experiments/        four narrower bindings, with their own README
└── tests/              conformance tests + the live L2 test
```

The runtime's two non-obvious engineering decisions, both learned the hard way:

**One round trip per tick.** Log delta, watched files, directory listing and liveness are fetched
by a single shell command with section markers. The naive four-calls-per-tick version observed a
ten-second job exactly once — which is a postmortem, not a surface.

**Every appended line is its own crossing.** A code that appends a step table is emitting a
series, not a state. Collapsing it to the latest value throws away the thing you wanted to see.

## Tests

```bash
python tests/test_seam.py           # conformance: 41 checks, no network
python tests/test_format_parity.py  # the formatting rule, implemented three times
SEAM_HOST=user@hpc python tests/test_live.py       # the L2 demo, ~60 s
python tests/test_bindings.py       # both surfaces + admission, against one live run
```

`test_seam.py` asserts the properties the specification makes normative:

- every shipped manifest validates, and both projections agree on every point, bound and enum
- `trigger` + `sealed` is rejected; a `dial` without finite bounds is rejected; `gated` +
  `sealed` is rejected; `gauge` is not a legal intervention affordance
- derivation refuses to guess: unbounded continuous → `unresolved`, free string → `unresolved`,
  boolean → `ratchet` (not a two-stop slider)
- liveness cannot be settled below E0/E1-channel/E4
- TOML ⇄ JSON round-trips without loss and without reordering points
- a manifest carrying an unknown key is an error, not a warning
- deck keys are block-qualified (`control.t_end` ≠ `laser.t_end`), units resolve
  (`femto → fs`), cross-references are not literals
- `Tag = Value` in a paramfile is flagged rather than accepted
- the trace is append-only, monotonic, and every intervention links to the preceding observation

---

## What is not claimed

**`seam` is a specification with a reference implementation.** A Gradio, Qt or web-components
binding would be equally valid; none of them would change a manifest, which is the only claim
worth making. `experiments/` exists to keep that honest rather than to ship six GUIs.

**Three of the four codes are `sealed` throughout.** If you came for live sliders over every
parameter, this project will disappoint you — deliberately. A control that does nothing while
appearing live is worse than no control.

**No SDF or HDF5 readers.** EPOCH's `.sdf` dumps and Gadget-2's HDF5 snapshots are declared as
artifacts but not parsed. Reading them would add a dependency and would not test the
specification. The `shape` and `pitch` declarations are there; a consumer with a reader can fill
them in.

**`pitch` has no event-driven variant.** EPOCH appends its step table once per snapshot write —
neither per-step nor per-second. Declared `per-step` with a `notes` entry, because hiding it
would be worse. A `per-event:<name>` pitch is a v0.2 candidate.

**Specimens are not signed.** They carry a manifest hash so a trace cannot be silently paired
with the wrong manifest, and nothing more. A specimen is evidence, not a proof.

**GPU is omitted, not declared.** `Simulation(; device=GPU())` exists, and the Tesla P100 is
present, but the package's CUDA path was not exercised here. It is therefore absent from
`manifests/astronbodysim.toml` rather than declared a ratchet over `{CPU, GPU}`: a control that
fails ten minutes into a run is worse than no control, and an undeclared capability is at least
visible as an absence.

## Version

Implements **Seam Specification v0.1-draft**. The specification repository is the authority on
what the words mean; this repository is evidence that they can be implemented four times without
adding a fifth.

## License

MIT. The specification text is CC-BY-4.0.
