#!/usr/bin/env bash
# Build the gamut-correction LUT family: the axis no ImageConfig knob can reach.
#
# The correction LUT is a property of the Display, not of ImageConfig, so the
# per-image search could never touch it and none of the sixteen renderings judged
# so far varied it. Historically it is also the only change that moved a
# photograph from "terrible" to "ok".
#
# Two dials. `blend` is how far toward the fitted gamut correction to go, 0 being
# none and 1 full; `chroma-trim` is a global chroma reduction composed on top.
# The shipped LUT is blend 0.5, trim 0.85, at 33 steps.
#
# b50_t85 is the control, not a variant: same settings as the shipped LUT but
# built at 17 steps like everything else here. If it rates differently from the
# deployed one, the difference is LUT resolution and every other comparison on
# this page is confounded by it. Build it first so a failure there stops the run
# before eighty minutes have gone into the rest.

set -eu
PY=.venv/Scripts/python.exe
OUT=build/camcal/luts
mkdir -p "$OUT"

build() {  # name blend trim
  local name=$1 blend=$2 trim=$3
  if [ -f "$OUT/$name.npy" ]; then echo "== $name already built"; return; fi
  echo "== building $name (blend=$blend trim=$trim)"
  "$PY" tools/color_lut_build.py --model huessen_epf1301 \
      --blend "$blend" --chroma-trim "$trim" --out "$OUT/$name.npy" 2>&1 \
    | grep -viE "^\s*$" | tail -4
}

build b50_t85  0.5  0.85   # resolution control: the shipped settings at 17 steps
build b00_t85  0.0  0.85   # no gamut correction at all, trim only
build b25_t85  0.25 0.85
build b100_t85 1.0  0.85   # full correction
build b50_t70  0.5  0.70   # same correction, more global desaturation
build b50_t100 0.5  1.0    # same correction, no trim

echo "== done"; ls -la "$OUT"
