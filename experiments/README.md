# experiments

Four bindings that are not part of what this repository asks you to run. Each one exists to
answer a question the shipped surfaces could not, and each one left a rule behind in SPEC §4.7.
They are kept runnable so the claims can be re-checked, and they are kept out of the main tree
so that "two surfaces" stays literally true.

| Binding | Transport | Frames | The question it answered |
| :--- | :--- | :--- | :--- |
| ImGui (`implot_app.py`) | HTTP poll | raw RGB → GPU texture | do widgets work without a DOM, at 60 fps? |
| Electron (`electron/`) | SSE + fetch | PNG | does a desktop shell buy capability, or only convenience? |
| Tk (`tk_app.py`) | HTTP poll | PNG → `PhotoImage` | does widget identity survive a poll? |
| Makie (`makie/`, Julia) | TOML snapshot | RGB → `image!` | does the spec survive a second language and a third transport? |

None of them changes a manifest. That is the only claim worth making about them: if a fifth
binding needed a fifth endpoint, the surface would be under-specified.

## Running them

```bash
pip install -r experiments/requirements.txt          # imgui-bundle, for the ImGui one

python experiments/implot_app.py --url http://127.0.0.1:8770
python experiments/implot_app.py --url http://127.0.0.1:8770 --selftest   # data path, no window
python experiments/tk_app.py     --url http://127.0.0.1:8770              # needs Tcl/Tk

(cd experiments/electron && npm install && npm start)

# Julia: the bridge writes a TOML snapshot, Makie reads it and draws
python experiments/makie/bridge.py --url http://127.0.0.1:8770 --out experiments/makie/live &
julia --project=experiments/makie experiments/makie/seam_makie.jl --backend gl
julia --project=experiments/makie experiments/makie/seam_makie.jl --backend cairo --once \
      --dir experiments/makie/live --save data/makie-cairo.png

python experiments/tests/test_tk_stub.py            # widget logic against a stub toolkit
python experiments/tests/test_makie.py --gl         # cairo render, then a real GL window
```

`tests/test_bindings.py` picks the first two up automatically when `node` and `imgui-bundle`
are present; it skips them otherwise.

## Tk — the same widget twice

An immediate-mode surface rebuilds everything on every frame and cannot notice if a point's
identity churns. A retained-mode one visibly resets focus, cursor and scroll. Tk builds the tree
once and patches it, and `tests/test_tk_stub.py` asserts that against a stub toolkit: no
interpreter on this machine has Tcl/Tk, so the widget *logic* is verified and the pixels are not.

## Makie — a second language

The bridge writes a **TOML snapshot** plus a raw-RGB file, and Julia reads both with the `TOML`
stdlib alone — no HTTP client, no JSON package. That made it the cheapest possible test of
SPEC §2(b), "transport is out of scope".

It also produced §4.7(f), from a bug:

```julia
# the naive reactive binding, which sent forty interventions nobody made
on(slider.value) do t
    intervene(...)          #   job → surface wrote into the slider
end                         # → slider fires → intervene → new state → slider → …
```

The fix is a sync guard around the *write*, inside the handler is too late. And a third
implementation of the formatting rule (§4.7b) found a third bug within a minute: Julia's `log10`
returns a `Float64`, which `@sprintf` refuses as a precision argument.

| Tk, headless-stub verified | Makie, CairoMakie |
| :--- | :--- |
| `tests/test_tk_stub.py` | `data/makie-cairo.png` |

## Electron — the shell

`main.js` holds one deliberate oddity: the renderer boots inside Chromium with `--smoke`, reads
the live DOM, and reports tabs, transport, control count and loaded frame dimensions. Chromium
rendering is therefore verified; the window chrome is not.

## ImGui — unexercised on this machine

`pip` has no index access here, so `imgui-bundle` could not be installed and only the data path
runs (`--selftest`, no window). This is the one binding whose visual layer was never started,
and it is stated here rather than glossed. The two spec rules it produced are in the main
README, because they were bugs in this repository: a slider firing once per frame of a drag, and
a formatter that stripped trailing zeros in both implementations at once.
