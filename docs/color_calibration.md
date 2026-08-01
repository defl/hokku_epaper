# Measuring a panel's colour with a colorimeter

The dither pipeline is built on one array per screen model —
`palette_measured_rgb` in `python/hokku/screens/<model>/display.py`. Every
palette LUT, the hue gating, the dynamic-range anchors and the quality metrics
are derived from it (see [dithering.md](dithering.md) §1, §8).

Its provenance is weak. `bigme_f7` borrowed its values from a third party's
`default-palettes.json`; `seeedstudio_e1004` inherits huessen's wholesale;
huessen's own numbers have no recorded method. This document is how to replace
them with values measured on our own glass, and how to check whether the panel
actually mixes inks the way the renderer assumes.

---

## 1. What the instrument can and cannot do

E-paper is **reflective**. It has no emission of its own, so what a meter reads
is entirely a function of the light you put on it.

That splits instruments into two classes:

| Instrument | Lamp? | How to use it here |
|---|:---:|---|
| Calibrite ColorChecker Display / Display Plus, X-Rite i1Display Pro / Studio, ColorMunki Display | no | Emissive mode under your own controlled light, normalised against a white reference. |
| X-Rite i1Pro / i1Pro 2 / 3, ColorMunki Photo, i1Studio | yes | Reflective mode, 45°/0°, calibrate on the white tile. Absolute L\*a\*b\* directly. |

A colorimeter has no lamp and no reflective mode — ArgyllCMS will refuse
`spotread` in reflective mode on one. It is still a perfectly good tristimulus
sensor; you just have to supply and control the illumination yourself, and
divide out its brightness and colour cast afterwards. That is what
`tools/color_read.py` does.

**Two error sources to accept on the colorimeter path.** Its filters approximate
the CIE observer for *display* spectra, not for broadband light reflected off
pigment — expect a few ΔE of systematic error. And the result is only as good
as your knowledge of the white reference. Repeatability will be far better than
absolute accuracy, which is the right trade: the LUTs care mostly about where
the six anchors sit *relative to each other*.

---

## 2. The rig

```
        lamp
          \  45°
           \
            \        ┌──────────┐   meter, perpendicular,
             \       │  patch   │ ← a few mm off the glass
              ＿＿＿＿│＿＿＿＿＿＿│＿＿＿
                     panel
```

- **Light**: one stable, high-CRI broadband source at roughly 45° to the panel
  normal. A D50 viewing lamp is ideal; a good high-CRI LED works. Kill every
  other light in the room, including daylight — one illuminant only, and it
  must not change between the first patch and the last.
- **Geometry**: 45°/0° is the standard for reflective media and it keeps the
  lamp's specular reflection off the front lamination out of the aperture.
  Rotate the meter's **ambient diffuser out of the way** — that cap is for
  measuring room light and would integrate the whole hemisphere.
- **Consistency beats correctness**: clamp or jig the meter so every patch is
  read at the same distance and angle. A patch-to-patch geometry change is
  indistinguishable from a colour difference.
- **A white reference**, measured in the identical geometry. A ColorChecker
  white patch or a white calibration tile with known Lab. This is what makes
  the numbers mean something — see §5.

The generated target's patches are ~29 mm square on the F7 against a meter
aperture of roughly 10–15 mm, so there is ~7 mm of centring slack per side.

---

## 3. Generate and display the target

```powershell
python tools\color_target.py --model bigme_f7
```

Writes to `build/colorcal/`:

| File | Purpose |
|---|---|
| `colorcal_<model>.png` | Upload this to the server |
| `colorcal_<model>.json` | Patch manifest — feeds `color_read.py` |
| `colorcal_<model>.bin` | Packed panel bytes, for `screen_sim.py --file` |

The target is 15 patches on a 5×3 grid: six flat ink anchors, seven
Bayer-dithered ramp patches at exactly k/8 black-ink coverage, and repeats of
white and black at the end of the sequence as a drift check.

### Route A — over the serial console (preferred, Bigme F7)

If the unit runs firmware with the `frame` command, skip the server entirely:

```powershell
python tools\f7_send_frame.py --port COM9 --target
```

This pushes the exact bytes down the console UART (~17 s at 115200) and refreshes
the panel. Nothing about the picture depends on the server's configured preset,
on WiFi, or on which host the screen happens to point at — which is what you want
while metering, and what makes a measurement session reproducible.

`--cycle` walks every ink and then the target, which is also the quickest way to
confirm all six inks actually fire on a given unit.

### Route B — through the server

For screens without the `frame` command, upload the PNG and let the server render
it. To get it onto the glass **unmodified**:

1. Set the screen's preset to **`calibration_raw`**. This matters. The normal
   presets run autocontrast, gamma, CLAHE, unsharp mask and error diffusion —
   any one of which destroys a flat patch or shifts a ramp's coverage away from
   the k/8 it is supposed to be. `calibration_raw` neutralises the whole chain
   and uses `noop` quantisation, so each pixel maps to the ink it was painted
   in. `test_color_target.py` asserts bit-exactness through the real renderer,
   so this cannot silently rot.
2. Upload the PNG and pin it with **show next**.
3. Wait for the screen to poll (or power-cycle it) and refresh.

Sanity-check the glass before metering: the six anchor patches must be visibly
flat, with no speckle. If they are not, the preset did not take.

**Put the preset back afterwards** — photos rendered with `calibration_raw`
band badly.

---

## 4. Take the readings

`tools/color_read.py` prompts patch by patch; you paste a reading into it. It
deliberately does not drive the instrument: that keeps it working with any
meter and any vendor software, and means getting numbers is not blocked on the
ArgyllCMS USB driver swap (which on Windows displaces the X-Rite/Calibrite
driver, so you will be swapping back and forth if you also use i1Profiler).

If you are using ArgyllCMS, `spotread -e` gives emissive readings; paste the
whole result line, the XYZ triple is picked out of it. Bare `X Y Z` works too.

```powershell
python tools\color_read.py build\colorcal\colorcal_bigme_f7.json --white-ref-lab 96.5,-0.4,1.2
```

Order matters only in that the two repeat patches come last — they are the
drift check. Readings are saved as you go, so a session can be re-analysed with
`--from-file` without re-metering.

---

## 5. Why the white reference matters

Raw readings carry the lamp's brightness and colour cast. Dividing every patch
componentwise by a reading of the white reference cancels both at once (a von
Kries adaptation); multiplying back up by the reference's *known* Lab is what
preserves the difference between "the reference is perfectly white" and "the
reference is a real, slightly off-white object".

Without a reference you can pass `--relative-to-panel-white`, which normalises
against the panel's own white ink instead. Understand what that costs:

- panel white is **defined** as neutral L\*=100, so the white ink's real
  blue-grey cast — a thing the pipeline models — is thrown away;
- absolute lightness is meaningless, so the panel-white anchor that
  `compress_dynamic_range()` depends on cannot be obtained.

It still yields usable *relative* geometry for the coloured anchors. It is a
fallback, not the plan.

---

## 6. Reading the output

### Ink anchors

A table of measured L\*a\*b\* per ink, the sRGB triple, and **ΔE vs the values
currently in the repo** — i.e. how wrong the borrowed palette was. The `repeat`
column is the spread between the two readings of white and of black; if that is
not small (say under 1 ΔE), the session drifted and the rest of the numbers are
suspect.

Then a ready-to-paste `palette_measured_rgb` block. Inks you skipped are
carried through unchanged and marked `NOT MEASURED`.

### Tone response

This is the part that can change more than the anchors do.

Error diffusion in this codebase propagates error in sRGB units, so it
implicitly assumes that a patch which is `f` black by area reads as the
`f`-weighted **sRGB** average of the two inks. Physically, area mixing is
linear in **reflectance**, not in sRGB — those are materially different curves.
On top of that, real electrophoretic ink has a dot-gain analogue.

The report gives measured L\* against both predictions:

```
  black area  measured L*  pred L* (sRGB)    error  pred L* (refl)
       50.0%        54.65           45.49    +9.17           56.58
```

The `error` column is the actionable one: it is the systematic mid-tone error
the renderer is making on every image. Under ~2 L\* the linear assumption is
fine. Much above that and a transfer curve applied before dithering is worth
building — it would be a global quality win, not a per-image tweak.

---

## 7. After measuring

1. Paste the new `palette_measured_rgb` into the model's `display.py`.
2. Re-run the quality metrics — the LUTs, the DRC anchors and the metrics all
   move with the palette:
   `pytest -m time_intensive -k test_dither_quality_metrics`
3. Update the table in [dithering.md](dithering.md) §1 (measured RGB / Lab /
   hue / chroma per ink) and the benchmark table in §13.
4. Note in the model's `display.py` docstring that the values are now measured,
   with the date and the instrument — that provenance is exactly what is
   missing today.

`seeedstudio_e1004` currently inherits huessen's palette. If it is ever metered
and differs, override `palette_measured_rgb` on that subclass rather than
editing the huessen base.
