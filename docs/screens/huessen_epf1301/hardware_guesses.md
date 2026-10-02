# Hokku 13.3" WiFi E-Paper Frame — Hardware Guesses

Unverified items. Confirmed findings get moved to [`hardware_facts.md`](hardware_facts.md).

## Unconfirmed GPIO Map

| GPIO | Expected Function | Notes |
|------|------------------|-------|
| 3 | EPAPER_PWR_EN | Active HIGH — needs verification |
| 4 | CHG_EN1 | Charger enable, active LOW |
| 13 | CHG_EN2 | Charger enable, active LOW |

## Physical Buttons → GPIO

The frame has three physical buttons. Which physical button is on which GPIO has not been
tested on the bench; the mapping below is inferred (bench test tracked in
[issue #56](https://github.com/defl/hokku_epaper/issues/56)):

- **Power button → GPIO 1 (BUTTON_1)**, the only button our firmware reads. Two independent
  sources point here: the factory firmware's name for GPIO 1 is "power off", and a user
  reports that on a Wayfair 13.3" unit running Hokku firmware the power button is the one
  that shows the next image ([issue #18](https://github.com/defl/hokku_epaper/issues/18)).
- **The other two → GPIO 40 (BUTTON_2, factory "switch photo") and GPIO 39 (BUTTON_3,
  factory "restart wifi")**, in an unknown physical order. Neither is RTC-capable, so
  neither can wake the chip from deep sleep, and our firmware ignores both.
- **GPIO 12 (PWR_BUTTON)**: it is unknown whether any physical button drives it at all. It
  transitions with GPIO 14 on USB-host plug events (see `hardware_facts.md`), and its name
  may simply be the factory firmware's.

## Power Architecture

- **EPAPER_PWR_EN (GPIO3)**: likely controls display power supply — not yet confirmed
- **USB power**: may not be sufficient for display refresh without battery — untested in isolation

## Deep Sleep

- **Target sleep current**: ~8µA with RTC GPIO isolation — design target, not empirically measured

## Display — Cross-reference: Seeed T133A01 driver (reTerminal E1004)

Seeed Studio's official open-source driver for the reTerminal E1004
(`Seeed-Projects/Seeed_GxEPD2`, `examples/GxEPD2_reTerminal_E1004/GxEPD2_T133A01_1200x1600.{cpp,h}`)
targets a different panel (T133A01) but shows striking overlap with our confirmed hardware facts
(checked 2026-07-15):

- **Identical palette**: their `_native_map` — Black=0x00, White=0x01, Yellow=0x02, Red=0x03,
  Blue=0x05, Green=0x06 — is bit-for-bit our confirmed Spectra 6 nibble mapping
  (`hardware_facts.md` → Display).
- **Same command opcode set and grouping**: same UC81xx-style opcodes (0x74, 0xF0, 0x00 PSR,
  0x50 CDI, 0x60, 0x86, 0xE3 PWS, 0x61 TRES, 0x01 PWR, 0xB6, 0x06 BTST, 0xB7, 0x05, 0xB0, 0xB1),
  split the same way into "broadcast to both chips" vs "primary chip only" as our init sequence's
  two phases.
- **Same per-chip physical geometry**: their two chips also split left/right, each physically
  600 columns × 1600 rows (their own `HEIGHT=1600` constant + "left/right half of every row"
  header comments) — matching our CONFIRMED CTRL1=left/CTRL2=right, 600×1600-per-panel
  arrangement exactly.
- **Same TRES value for a "logical" resolution**: they program the identical TRES bytes we do
  (`{0x04,0xB0,0x03,0x20}` = 1200×800) to each chip, even though each chip's real physical output
  is 600×1600 — consistent with (not contradicting) our own note in `hardware_facts.md` that
  1200×800 is a *logical* resolution this controller's dual-gate driving remaps to a 600×1600
  *physical* panel. Not a copy-paste bug in their driver; it looks like the correct value for
  this controller/gate-driving architecture.

**Inference** (unconfirmed — no schematic/datasheet cross-reference, only independently
converging driver behavior): huessen_epf1301's EL133UF1-based board and the reTerminal E1004's
T133A01 board likely share the same or a closely related 13.3" Spectra 6 controller + dual-gate
driving engine (probably a UC8179-family IC), integrated by different OEM/board vendors.

## USB Detection — Mechanism Inference

Why GPIO14 behaves as a host-detect rather than VBUS-detect is inferred, not schematic-confirmed:

GPIO 14 is likely wired to a charger-IC pin that asserts only after USB-BC (Battery
Charging Specification 1.2) detection determines the source is a Standard Downstream
Port (SDP) or similar data-capable host. Pure-power sources (Dedicated Charging Port /
wall warts / battery banks) don't trigger BC detection because there's no data signaling
on D+/D-, so the pin stays de-asserted (HIGH). The charger IC identity is unknown.
