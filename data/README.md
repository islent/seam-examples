# data

Inputs and evidence. Nothing here is generated at test time — these are the actual files the
reference runs consumed or produced, kept so that a claim in the README can be checked without
running anything.

| File | What it is | Which claim it supports |
| :--- | :--- | :--- |
| `epoch_laser.deck` | a real EPOCH input deck, copied from the code's own test suite | `seam derive --kind deck` parses a real deck, not a toy one |
| `gadget2_param.txt` | a real Gadget-2 paramfile | the `Tag<whitespace>Value` rewriting rule, and the `=` trap |
| `derived-epoch.toml` | output of `seam derive` on `epoch_laser.deck` | 11 of 14 points come back `unresolved`; that is the honest result |
| `meep-slice.npy` | a raw 160×160 `Ez` array from a driven point source | the grid-scale checkerboard in §4.6 of the spec |
| `meep-slice-frame.png` | that array rendered at native resolution | what the unreduced field looks like — structure that is not there |
| `meep-frame-w80.png` | the same array after 2×2 box averaging | downsampling is a filter: the mode cancels exactly |
| `meep-frame-w40.png`, `meep-frame-w160.png` | the same at other widths | the reduction is declared once, not chosen per viewer |
| `meep-live-frame.png` | a frame fetched from `/api/frame` during a live run | the preview path works against a running job |
| `shot-field.png` | screenshot of the web surface, Field tab | a live field crossing the boundary at the declared width |
| `shot-surface.png` | screenshot of the web surface, Surface tab | sealed controls are visibly disabled, live ones are not |
| `shot-groups.png` | the same after grouping became declarative | controls appear in manifest order, grouped as declared |
| `makie-cairo.png` | CairoMakie render of the whole surface | the Julia binding draws the same numbers as the Python ones |
| `makie-gl.png` | frame from a real GLMakie window | the GL path opened, refreshed, and closed (not just a headless render) |

Two things are deliberately **not** here: the full traces (they live in `specimens/`, where the
hashes are checked) and the screenshot tool. `tests/shot_ui.py` regenerates the `shot-*.png`
files against a live run; it needs Playwright.
