# Electron host

The renderer is `../ui/index.html` — the *same* file the seam process serves to a browser. This
directory adds only what a browser cannot: a resident window, disk access for specimens and
post-processing inputs, and a configurable API base.

```bash
npm install
SEAM_BASE=http://127.0.0.1:8770 npm start
```

## What is verified, and what is not

`node selftest.js --url http://127.0.0.1:8770` exercises the exact data path the renderer uses —
manifest, state, SSE, raw-RGB frames, and the rejection of a write to an unknown point — using
nothing but Node's standard library. It passes against a live Meep run.

The Chromium window itself is **not verified in this repository's development environment**:
`npm` resolves but Electron's binary was not downloaded, and a window needs a display. So what is
untested is window chrome and menu wiring, not semantics.

If the two disagree, the semantics win: the manifest is the contract, and this shell is one
consumer of it.

## Why this exists at all

Not because the surface needs a desktop shell — it does not. Because `Binding` is the claim that
the surface is a projection of the manifest rather than a web app, and a claim with exactly one
example is not a claim, it is a description. `../seam/bindings.md` records all three consumers and
the admission rule they must share.
