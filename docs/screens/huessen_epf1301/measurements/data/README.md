# Huessen EPF1301 colour campaign — raw dataset

The complete measurement record from the Huessen EPF1301 characterisation
campaign: **1733 readings over 20 sessions**, covering **all 1505** planned
patches — nothing missing. Every reading carries its full reflectance spectrum.

This directory is the primary record. It exists so that any later analysis — a
3-D correction LUT, a different observer, a different illuminant — can be redone
without putting a meter back on glass. Reproducing it costs roughly 17 hours of
panel time spread across 20 human-attended calibration cycles, so it is stored
rather than regenerated.

Structured and measured the same way as the [Bigme F7's campaign](../../bigme_f7/measurements/data/)
— same spec generator, same runner, same instrument protocol — so the two
datasets are directly comparable field-for-field. See `findings.md` in the
parent directory for the write-up and the head-to-head numbers.

## Files

| file | what it is |
|---|---|
| `campaign.jsonl` | one JSON object per line; the measurements |
| `campaign_spec.json` | the exact patch plan that was run (`spec_sha256` in the data ties to it) |

## Provenance

X-Rite ColorMunki Photo via ArgyllCMS 3.5.0 `spotread -O -N -s -i D65`,
reflective 45°/0°. Huessen EPF1301 firmware 1.2.25, one unit, dial-calibrated
before every session.

The meter was **clamped in one position and never moved for the whole
campaign**, so every patch fills the entire panel. Patches were uploaded over
USB with the `frame` protocol — never served over HTTP. Patch aiming is
therefore not an error source anywhere in this dataset.

Unlike the F7 (bridged over a CH340 at a hard 115200-baud ceiling), this board's
console is native USB Serial/JTAG — uploads ran at ~790 KiB/s, about 15× the
F7's transfer speed. The larger difference in practice was **USB-interactive
mode** (`firmware/common/all/interactive.h`): the device holds its console open
and suspends its own refresh schedule for as long as a host asserts the mode,
re-checked and re-recorded before every single upload (see
`interactive_confirmed` below) rather than caught once per session like the F7
required.

## Schema

Not every key appears on every row; rows are one of three kinds, distinguished
by `phase` and `anchor_tag`.

**Measurement identity**

| field | meaning |
|---|---|
| `uid` | stable patch id; the resume key |
| `phase` | `inks`, `tone_fine`, `lut_gain`, `algo_gain`, `lut_algo_cross`, `skin`, `gamut_dense`, `control` |
| `label` | human-readable description |
| `rgb` | source colour fed to the dither |
| `config` / `config_name` | the `DitherConfig` used (algorithm, LUT, serpentine, hue cutoff, neutral chroma) |
| `ink_index` / `ink_fraction` | set on `inks` and Bayer-ramp rows |
| `bayer_k` | Bayer level k/8, where applicable |
| `raster_sha1` | SHA-1 of the dithered raster actually displayed |
| `is_control` | true for the interleaved drift-instrumentation repeats |

**The measurement**

| field | meaning |
|---|---|
| `xyz_d65_pct` | Bradford-adapted to D65, Y=100 for a perfect diffuser — the headline value |
| `median_raw` | median of the repeats, exactly as the instrument reported (D50) |
| `raw` | every individual repeat |
| `spectrum_pct` / `median_spectrum_pct` | reflectance %, 36 bands |
| `wavelengths_nm` | 380–730 nm, parallel to the spectra |
| `repeats` | how many reads were averaged (3 for spec patches) |

The spectra are the part that **cannot be reconstructed**. XYZ is a projection of
the spectrum through one observer and one illuminant; a session recorded without
spectra can never answer "how does this look under tungsten?" without going back
to the hardware.

**Provenance per row**

`t_unix`, `session`, `settle_s`, `instrument`, `instrument_args`, `spec`,
`spec_sha256`, `model`, `firmware`, `battery_mv`, `battery_mv_start`,
`anchor_tag` (`open`/`close` on the per-session primaries brackets),
`duplicate_of` (see below), `error`, `record`, `source`,
`interactive_confirmed` (see below).

**`firmware` is empty on every row of this dataset.** `read_firmware()` fetches
it via the F7's `cfg show` console command, which this board's firmware does not
implement; the call fails silently and the field is never populated. Not a
redaction — there was never anything there to remove. Firmware identity for this
campaign is `huessen_epf1301` v1.2.25, fixed for the whole run and recorded here
in prose instead.

**`interactive_confirmed`** is a defensive addition this campaign needed and the
F7's did not: whether the device confirmed USB-interactive mode was engaged
immediately before *this specific* upload, not just once at session start.
Added mid-campaign after the device was observed resuming its own scheduled
refresh — fetching a real image from its configured server — during the idle gap
between sessions (dial rotation + meter repositioning), silently clearing the
RAM-only interactive flag. Re-asserting once per session was not enough to catch
a reset that could happen at any point. Checked here rather than assumed:
**`interactive_confirmed` is `true` on all 1733 rows** — the defence held for
the entire campaign, and every reset that did occur was caught and recovered
before the next upload, not during one.

## Two things a reader will otherwise get wrong

**`duplicate_of` rows were not measured.** Many planned patches dither to a
raster byte-identical to one already measured — different source colours, same
ink pattern. Those inherit the earlier reading rather than re-displaying it, and
say so. It was validated: 4814 independent pairs that were measured twice
anyway agree to mean ΔE 0.896 (median 0.658), close to the measurement noise
floor below. Treat inherited rows as real data, but do not count them as
independent samples.

Control patches are excluded from inheritance in both directions — they exist to
measure drift, and a control that silently reused an old reading would report
zero drift by construction.

**Sessions are bracketed, and the brackets are the error bars.** Every session
re-measures all six solid inks at open and close (`anchor_tag`). Across 20
sessions those brackets converge at **0.656 ΔE mean, p95 1.560** — noticeably
higher than the F7's 0.35 ΔE, and not evenly spread across inks: black/white/blue
sit around 0.4–0.5 ΔE, while **red (worst 4.09 ΔE) and green (mean 1.17) are
measurably less stable**, a real panel characteristic rather than a small-sample
artifact (built from 38 brackets per ink, more than the F7's eventual count).
**Any conclusion drawn from this dataset that rests on a difference smaller than
~0.66 ΔE is inside the noise** — a stricter bar than the F7's dataset required.

## Coverage

**1505 of 1505 spec patches — complete.** No gaps.

## Caveats

- **One unit, one meter, one clamp position.** Panel-to-panel spread is
  unmeasured. These are this screen's numbers, not the model's.
- **The meter rests on the glass.** 45°/0° rejects the specular component, which
  is correct, but contact pressure on a flexible panel may shift the surface.
  Unquantified.
- **Red and green are less stable than the other four inks on this panel** —
  see the noise-floor breakdown above. Any red- or green-heavy conclusion needs
  a wider margin than one built on black, white or blue.
- **Dither gate parameters matter more than they look.** `hue_aware` at the
  shipped gate (`hue_cutoff_deg=95`, `neutral_chroma=8`) is bit-identical to
  `euclidean` on skin tones on the F7; not independently re-verified on this
  board, but the config is recorded in full on every row for exactly this
  reason — never compare LUTs without checking the gate.

See [`../findings.md`](../findings.md) for the full write-up and the head-to-head
comparison against the F7.
