# SPEC 4.7b, third implementation — and deliberately standalone.
#
# This file exists apart from seam_makie.jl so that the formatting rule can be tested without
# loading Makie: a rule that can only be checked by starting a GPU window is a rule nobody checks.
#
# Julia's `Printf` is stdlib, so no dependency is added for this.

module SeamFormat

using Printf

export seam_format, with_unit

"""
    seam_format(value, precision=4)

Significant-digit rendering, matching seam.client.format_value (Python) and fmtValue in
ui/index.html (JavaScript). The rule is numeric rather than type-based because TOML, JSON and
Julia's own `Real` all erase the int/float distinction before a binding sees a value.
"""
function seam_format(v, p::Int = 4)
    v === nothing && return "—"
    v isa Bool && return v ? "true" : "false"
    v isa AbstractString && return v
    x = try
        Float64(v)
    catch
        return string(v)
    end
    isfinite(x) || return string(x)
    p == 0 && return string(round(Int, x))
    x == 0 && return "0"

    if isinteger(x) && abs(x) < 1e18
        digits = length(string(abs(Int(x))))
        digits <= p && return string(Int(x))
    end

    # Int, not Float: @sprintf rejects a Float64 precision argument.
    m = Int(floor(log10(abs(x))))
    if m >= p || m < -2
        # Trailing zeros in the mantissa carry the declared precision; do not strip them.
        s = @sprintf("%.*e", max(0, p - 1), x)
        mant, expo = split(s, 'e')
        return string(mant, "e", parse(Int, expo))
    end

    dec = max(0, p - 1 - m)
    dec == 0 && return string(round(Int, x))
    s = @sprintf("%.*f", dec, x)
    if occursin('.', s)
        s = rstrip(s, '0')
        s = rstrip(s, '.')
    end
    return s
end

"""Append a unit, as every binding does, so unit placement is not a per-binding choice."""
with_unit(unit, text) = isempty(string(unit)) ? text : string(text, " ", unit)

end # module
