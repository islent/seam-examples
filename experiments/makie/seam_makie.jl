#!/usr/bin/env julia
# Makie binding for a Seam surface.
#
# This is the fifth binding and the third *programming model*: the web is DOM, ImGui is
# immediate-mode, Tk is retained-mode widget trees, and Makie is reactive — an `Observable` graph
# where setting a value redraws whatever depends on it. Makie is also the first binding that lives
# in a different language, which is the actual point: the seam is a declaration, so a Julia process
# reading a TOML snapshot should be able to drive a Meep run as well as a Python process can.
#
# Transport: a file, written by bridge.py. No HTTP.jl, no JSON package. SPEC §2(b) claims transport
# is out of scope; this is the cheapest proof of that claim.
#
#   julia --project=experiments/makie experiments/makie/seam_makie.jl --backend gl
#   julia --project=experiments/makie experiments/makie/seam_makie.jl --backend cairo --once --out data/makie.png
#
# Interactive bits that matter for the spec:
#   * SPEC 4.7b formatting is implemented a third time here (seam_format) and compared against the
#     Python and JavaScript ones by tests/test_format_parity.py.
#   * SPEC 4.7c commit boundary: dragging a slider only records a pending value; releasing the
#     mouse writes exactly one command.
#   * SPEC 4.7e arming: a `confirm` trigger needs two clicks, and shows that it is armed.

using TOML
using Printf

const BACKEND = let i = findfirst(==("--backend"), ARGS)
    i === nothing ? "gl" : ARGS[i + 1]
end
const ONCE = "--once" in ARGS
const OUT = let i = findfirst(==("--save"), ARGS)
    i === nothing ? "makie-surface.png" : ARGS[i + 1]
end

if BACKEND == "cairo"
    using CairoMakie
    const MK = CairoMakie
else
    using GLMakie
    const MK = GLMakie
end

# ---------------------------------------------------------------------------
# SPEC 4.7b, third implementation
# ---------------------------------------------------------------------------

# The formatting rule lives in SeamFormat.jl so it can be tested without loading Makie, and so
# that the implementation the tests check is the implementation this binding runs.
include(joinpath(@__DIR__, "SeamFormat.jl"))
using .SeamFormat


clip_text(s::AbstractString, n::Int) = length(s) <= n ? s : string(first(s, n - 1), "…")

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

struct Point
    name::String
    affordance::String
    liveness::String
    grade::String
    group::String
    unit::String
    doc::String
    disabled::Bool
    confirm::Bool
    precision::Int
    value::Any
    domain::Dict{String,Any}
end

struct Snapshot
    name::String
    code::String
    status::String
    steps::Int
    groups::Vector{String}
    points::Vector{Point}
    observes::Vector{Point}
    series::Dict{String,Vector{Float64}}
    frame_file::String
    frame_point::String
end

function load_snapshot(dir::String)::Union{Snapshot,Nothing}
    path = joinpath(dir, "state.toml")
    isfile(path) || return nothing
    data = try
        TOML.parsefile(path)
    catch
        return nothing   # a half-written snapshot is not an error, it is the next tick
    end
    seam = get(data, "seam", Dict{String,Any}())
    points = Point[]
    for p in get(data, "point", [])
        push!(points, Point(
            string(get(p, "name", "")), string(get(p, "affordance", "")),
            string(get(p, "liveness", "")), string(get(p, "grade", "")),
            string(get(p, "group", "")), string(get(p, "unit", "")),
            string(get(p, "doc", "")), Bool(get(p, "disabled", false)),
            Bool(get(p, "confirm", false)), Int(get(p, "precision", 4)),
            get(p, "value", nothing), Dict{String,Any}(get(p, "domain", Dict{String,Any}())),
        ))
    end
    obs = Point[]
    series = Dict{String,Vector{Float64}}()
    for o in get(data, "observe", [])
        push!(obs, Point(
            string(get(o, "name", "")), string(get(o, "shape", "")),
            string(get(o, "pitch", "")), "", string(get(o, "group", "")),
            string(get(o, "unit", "")), "", true, false,
            Int(get(o, "precision", 4)), get(o, "value", nothing), Dict{String,Any}(),
        ))
        series[string(get(o, "name", ""))] = Float64[Float64(x) for x in get(o, "series", [])]
    end
    return Snapshot(
        string(get(seam, "name", "seam")), string(get(seam, "code", "")),
        string(get(seam, "status", "")), Int(get(seam, "steps", 0)),
        String[string(g) for g in get(seam, "groups", String[])],
        points, obs, series, joinpath(dir, "frame.bin"),
        string(get(seam, "frame_point", "")),
    )
end

function write_cmd(dir::String, point::String, value)
    io = open(joinpath(dir, "cmd.toml"), "w")
    println(io, "# one pending intervention, written by the Makie binding")
    println(io, "point = \"", point, "\"")
    if value isa AbstractString
        println(io, "value = \"", value, "\"")
    elseif value isa Bool
        println(io, "value = ", value)
    else
        println(io, "value = ", seam_format(Float64(value), 12), "")
    end
    close(io)
end

# SPEC 4.7d: position in [0,1] to value, linear or geometric
pos_to_value(dom, t) = begin
    lo = Float64(get(dom, "lo", 0.0)); hi = Float64(get(dom, "hi", 1.0))
    logscale = get(dom, "log", false) === true
    v = logscale ? lo * (hi / lo)^clamp(t, 0, 1) : lo + (hi - lo) * clamp(t, 0, 1)
    get(dom, "dtype", "") == "int" ? round(Int, v) : v
end

value_to_pos(dom, v) = begin
    lo = Float64(get(dom, "lo", 0.0)); hi = Float64(get(dom, "hi", 1.0))
    logscale = get(dom, "log", false) === true
    x = clamp(Float64(v), lo, hi)
    logscale ? log(x / lo) / log(hi / lo) : (hi == lo ? 0.0 : (x - lo) / (hi - lo))
end

# ---------------------------------------------------------------------------
# the surface
# ---------------------------------------------------------------------------

mutable struct Surface
    dir::String
    fig::Any
    snap::Union{Snapshot,Nothing}
    signature::Vector{String}
    syncing::Base.RefValue{Bool}
    pending::Base.RefValue{Any}
    armed::Set{String}
    slider_widgets::Dict{String,Any}
    value_labels::Dict{String,Any}
    armed_labels::Dict{String,Any}
    field_ax::Any
    field_obs::Any
    spark_axes::Dict{String,Any}
    layout::Any
    header::Any
end

function Surface(dir::String)
    # Figure height follows the content: Makie clips anything past the declared size, so a
    # fixed 800 px cut the first two controls off the top of the first render.
    snap0 = load_snapshot(dir)
    n = snap0 === nothing ? 8 : length(snap0.points)
    # Over-provision height: Makie clips rather than grows, so an underestimate hides the
    # top rows instead of making the figure taller.
    cols_h = 260 + 84 * max(5, n)
    fig = MK.Figure(size = (1280, cols_h), backgroundcolor = :white, fontsize = 12)
    surface = Surface(dir, fig, nothing, String[], Ref(false), Ref{Any}(nothing), Set{String}(),
                      Dict{String,Any}(), Dict{String,Any}(), Dict{String,Any}(),
                      nothing, nothing, Dict{String,Any}(), nothing, nothing)
    refresh!(surface; force = true)
    return surface
end

function signature(snap::Union{Snapshot,Nothing})::Vector{String}
    snap === nothing && return String[]
    vcat([string(p.name, "/", p.affordance) for p in snap.points],
         [string(o.name) for o in snap.observes])
end

function refresh!(s::Surface; force::Bool = false)
    snap = load_snapshot(s.dir)
    snap === nothing && return
    sig = signature(snap)
    if force || sig != s.signature
        s.snap = snap
        s.signature = sig
        build!(s, snap)
    else
        s.snap = snap
    end
    sync!(s, snap)
end

function build!(s::Surface, snap::Snapshot)
    fig = s.fig
    empty!(fig)
    s.slider_widgets = Dict{String,Any}()
    s.value_labels = Dict{String,Any}()
    s.armed_labels = Dict{String,Any}()
    s.spark_axes = Dict{String,Any}()

    left = MK.GridLayout()
    right = MK.GridLayout()
    fig[1, 1] = left
    fig[1, 2] = right
    MK.colgap!(fig.layout, 28)
    MK.rowgap!(fig.layout, 12)

    # Identity lives inside the left column. A separate header row collided with the first
    # control in two attempts: Makie clips rather than grows, so a fixed header row plus a
    # taller-than-figure column overlap instead of pushing each other apart.
    s.header = MK.Label(left[1, 1], snap.name * "   ·   " * snap.code,
                        halign = :left, fontsize = 15, font = :bold)
    MK.Label(left[2, 1], snap.status * "  ·  steps " * string(snap.steps),
             halign = :left, fontsize = 10, color = :gray40)
    row = 4
    for group in unique([p.group for p in snap.points])
        MK.Label(left[row, 1], isempty(group) ? "INTERVENTIONS" : uppercase(group),
                 halign = :left, fontsize = 10, color = :gray45, font = :bold)
        row += 1
        for p in snap.points
            p.group == group || continue
            cell = MK.GridLayout()
            left[row, 1] = cell
            MK.Label(cell[1, 1], p.name, halign = :left, font = :bold, fontsize = 12)
            MK.Label(cell[1, 2], p.liveness, halign = :left, fontsize = 9,
                     color = p.liveness == "sealed" ? :gray55 :
                             p.liveness == "midrun" ? RGBf(0.12, 0.45, 0.25) : RGBf(0.7, 0.42, 0.05))
            if !isempty(p.doc)
                # One line, truncated. A three-row card per control overflows any figure that
                # also has to hold a field image.
                MK.Label(cell[1, 3], clip_text(p.doc, 58), halign = :left, fontsize = 8,
                         color = :gray50)
            end
            MK.Label(cell[1, 4], p.disabled ? "not writable" : "", halign = :right,
                     fontsize = 9, color = :gray55)

            if p.affordance == "dial"
                dom = p.domain
                lo = Float64(get(dom, "lo", 0.0))
                sl = MK.Slider(cell[2, 1:2], range = 0.0:0.001:1.0,
                               startvalue = value_to_pos(dom, p.value === nothing ? lo : p.value))
                lbl = MK.Label(cell[2, 4], "", halign = :right, fontsize = 12)
                s.slider_widgets[p.name] = sl
                s.value_labels[p.name] = lbl
                on(sl.value) do t
                    s.syncing[] && return
                    v = pos_to_value(dom, t)
                    lbl.text[] = with_unit(p.unit, seam_format(v, p.precision))
                    # SPEC 4.7c: record, do not send. One gesture, one crossing, on release.
                    s.pending[] = (p.name, v)
                end
            elseif p.affordance == "ratchet"
                vals = get(p.domain, "values", Any[])
                if isempty(vals)
                    MK.Label(cell[2, 1:4], "no admissible values in this snapshot",
                             halign = :left, fontsize = 10, color = RGBf(0.7, 0.3, 0.1))
                else
                    # Makie's Menu takes option *labels*; passing Pairs renders `"1" => "1"`.
                    m = MK.Menu(cell[2, 1:2], options = [string(v) for v in vals])
                    MK.Label(cell[2, 4], string(p.value), halign = :right, fontsize = 12)
                    on(m.selection) do sel
                        s.syncing[] && return
                        idx = findfirst(==(string(sel)), [string(v) for v in vals])
                        idx === nothing || write_cmd(s.dir, p.name, vals[idx])
                    end
                end
            elseif p.affordance == "trigger"
                b = MK.Button(cell[2, 1], label = "fire"; fontsize = 11, height = 26)
                on(b.clicks) do _
                    if p.confirm && !(p.name in s.armed)
                        push!(s.armed, p.name)
                        b.label[] = "confirm"
                        return
                    end
                    delete!(s.armed, p.name)
                    b.label[] = "fire"
                    write_cmd(s.dir, p.name, true)
                end
            else
                MK.Label(cell[2, 1:4], "unresolved — no affordance could be settled",
                         halign = :left, fontsize = 11, color = RGBf(0.70, 0.15, 0.12))
            end
            row += 1
        end
        row += 1
    end

    # --- observations ---------------------------------------------------------------------
    orow = 1
    for group in unique([o.group for o in snap.observes])
        MK.Label(right[orow, 1:2], isempty(group) ? "OBSERVATIONS" : uppercase(group),
                 halign = :left, fontsize = 10, color = :gray45, font = :bold)
        orow += 1
        for o in snap.observes
            o.group == group || continue
            cell = MK.GridLayout()
            right[orow, 1:2] = cell
            MK.Label(cell[1, 1], o.name, halign = :left, fontsize = 11, font = :bold)
            MK.Label(cell[1, 2], with_unit(o.unit, seam_format(o.value, o.precision)),
                     halign = :right, fontsize = 12)
            orow += 1
            if o.affordance == "grid"
                if o.name != snap.frame_point
                    # Only the point the bridge actually fetched has pixels. Binding an image
                    # axis to an empty observable draws a black rectangle that looks like data.
                    MK.Label(right[orow, 1:2], "no preview in this snapshot",
                             halign = :left, fontsize = 10, color = :gray55,
                             height = 28)
                    orow += 1
                    continue
                end
                # The preview arrives at ~80x80; give the axis a real height or DataAspect
                # shrinks it to a thumbnail.
                # No axis title: the observation label above already names it, and a second
                # copy reads like two different quantities.
                ax = MK.Axis(right[orow, 1:2], height = 240, aspect = MK.DataAspect(),
                             yreversed = true)
                ax.xticksvisible = false; ax.yticksvisible = false
                ax.xticklabelsvisible = false; ax.yticklabelsvisible = false
                ax.bottomspinevisible = false; ax.leftspinevisible = false
                s.field_ax = ax
                s.field_obs = MK.Observable(zeros(RGBf, 2, 2))
                MK.image!(ax, s.field_obs)
            else
                ax = MK.Axis(right[orow, 1:2], height = 62, yticklabelsize = 8)
                ax.xticksvisible = false; ax.xticklabelsvisible = false
                ax.yminorticksvisible = false
                ax.bottomspinevisible = false; ax.rightspinevisible = false
                ax.topspinevisible = false
                MK.lines!(ax, Float64[0.0, 0.0], color = RGBf(0.18, 0.44, 0.82), linewidth = 1.4)
                s.spark_axes[o.name] = ax
            end
            orow += 1
        end
        orow += 1
    end

    # mouse release anywhere = commit (SPEC 4.7c)
    on(MK.events(fig).mousebutton) do event
        if event.button == MK.Mouse.left && event.action == MK.Mouse.release
            pend = s.pending[]
            if pend !== nothing
                write_cmd(s.dir, pend[1], pend[2])
                s.pending[] = nothing
            end
        end
    end
    nothing
end

function sync!(s::Surface, snap::Snapshot)
    s.syncing[] = true
    try
        s.header.text[] = snap.name * "   ·   " * snap.code * "      " * snap.status *
                          "  ·  steps " * string(snap.steps)
        for p in snap.points
            haskey(s.slider_widgets, p.name) || continue
            lbl = s.value_labels[p.name]
            lbl.text[] = with_unit(p.unit, seam_format(p.value, p.precision))
            if s.pending[] === nothing || s.pending[][1] != p.name
                s.slider_widgets[p.name].value[] = value_to_pos(p.domain, p.value)
            end
        end
        for o in snap.observes
            haskey(s.spark_axes, o.name) || continue
            ys = get(snap.series, o.name, Float64[])
            length(ys) >= 2 || continue
            ax = s.spark_axes[o.name]
            empty!(ax)
            MK.lines!(ax, ys, color = RGBf(0.18, 0.44, 0.82), linewidth = 1.4)
        end
        # frame
        if s.field_obs !== nothing && isfile(snap.frame_file)
            bytes = read(snap.frame_file)
            nl = findfirst(==(UInt8('\n')), bytes)
            if nl !== nothing
                head = split(String(bytes[1:nl-1]))
                if length(head) >= 2
                    w = parse(Int, head[1]); h = parse(Int, head[2])
                    body = bytes[nl+1:end]
                    if length(body) == w * h * 3
                        mat = Array{RGBf}(undef, w, h)
                        idx = 1
                        for j in 1:h, i in 1:w
                            mat[i, j] = RGBf(body[idx] / 255, body[idx+1] / 255, body[idx+2] / 255)
                            idx += 3
                        end
                        s.field_obs[] = permutedims(mat, (2, 1))
                    end
                end
            end
        end
    finally
        s.syncing[] = false
    end
end

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

function main()
    dir = let i = findfirst(==("--dir"), ARGS)
        i === nothing ? joinpath(@__DIR__, "live") : ARGS[i + 1]
    end
    s = Surface(dir)
    if ONCE
        # headless: one snapshot, one image. CairoMakie only.
        MK.save(OUT, s.fig)
        println("wrote ", OUT)
        return
    end
    if BACKEND != "gl"
        error("interactive mode needs --backend gl; use --once for cairo")
    end
    smoke = let i = findfirst(==("--gl-smoke"), ARGS)
        i === nothing ? 0.0 : parse(Float64, ARGS[i + 1])
    end
    if smoke > 0
        # Open a real GL window, refresh it a few times, close it. This exercises window
        # creation and GPU texture upload for the field image; a screen capture would exercise
        # the same thing plus somebody's desktop, which is not mine to photograph.
        MK.save(joinpath(@__DIR__, "..", "..", "data", "makie-gl.png"), s.fig)
        display(s.fig)
        println("GL window: opened")
        t0 = time()
        while time() - t0 < smoke
            refresh!(s)
            sleep(0.6)
        end
        println("GL window: refreshed ", round(Int, smoke / 0.6), " times")
        try
            MK.closeall()
        catch
            # closeall is the documented teardown; a backend without screens has nothing to do
        end
        println("GL window: closed, status ", s.snap === nothing ? "no-snapshot" : s.snap.status)
        return
    end
    display(s.fig)
    @async while isopen(stdout)
        try
            refresh!(s)
        catch e
            @warn "refresh failed" exception = e
        end
        sleep(0.8)
    end
    wait(s.fig)
end

if abspath(PROGRAM_FILE) == @__FILE__
    main()
end
