# Seeed reTerminal E1004 — Hardware Guesses

Unverified / not-E1004-confirmed items. Most come from the **shared reTerminal
E-Series baseboard** as documented for the E1002 sibling (Zephyr devicetree) and
the Seeed ESPHome cookbook (which covers the E-Series including E1004), NOT from
an E1004-specific schematic. Strong inference, but confirm on a physical E1004 or
its schematic PDF before relying on any of it. Confirmed items live in
[`hardware_facts.md`](hardware_facts.md). (Per root `AGENTS.md` "Hardware" rule.)

## Buttons (DOCUMENTED for E-Series / INFERRED for E1004)
Three momentary buttons on the baseboard, all INPUT_PULLUP, active-LOW, all on
RTC-capable GPIOs → all usable as EXT1 deep-sleep wake sources.

| Button | GPIO | Notes |
|--------|------|-------|
| Green / "Refresh" | GPIO3 | active-low |
| White A | GPIO4 | active-low — the **stock deep-sleep wake pin** (Seeed ESPHome low-power example + Zephyr both wake on GPIO4) |
| White B | GPIO5 | active-low |

- Left/right silk-screen labels for GPIO4/GPIO5 are ambiguous across sources (Zephyr says GPIO4=left, ESPHome says GPIO4=right); the **GPIO numbers agree**. Confirm physically if a specific button matters.
- Source: Zephyr `reterminal_e1002_procpu.dts` gpio-keys; Seeed ESPHome cookbook.

## USB / external-power detect (DOCUMENTED-absent as a GPIO)
- **There is NO GPIO for VBUS/USB-present.** External-power state is only readable over I²C from the charger's power-good/status register.
- Consequence for firmware: the `huessen_epf1301`-style "USB_AWAKE vs BATTERY_IDLE regime driven by a USB-detect GPIO" does **not** port to this board. Options are (a) a deep-sleep appliance woken by timer + buttons (chosen for the initial firmware), or (b) poll the SY6974B over I²C on timer wakes to detect external power. There is no RTC-GPIO to EXT1-wake on "charger plugged in".

## Battery charger (DOCUMENTED for E-Series / INFERRED for E1004)
- **Charger IC**: Silergy SY6974B — 3A single-cell Li-ion switching charger, I²C control, USB BC1.2 detection, power-path management.
- **I²C address**: 0x6B on I2C1 (SDA GPIO39 / SCL GPIO40).
- Charge status and charge-enable are **I²C registers, not GPIOs**. The charger `/INT` pin is not wired to any ESP32 GPIO on stock hardware (no interrupt route → no wake-on-charge).
- Source: Zephyr `silergy,sy6974b` binding + E1002/E1003 board docs.

## Panel ENABLE (GPIO12) semantics (INFERRED)
- The Seeed example only drives GPIO12 HIGH before init and treats it as the panel enable. Best inference: it gates the EPD panel-power / boost rail — drive HIGH to power the display for a refresh, LOW to cut display power between refreshes (standard e-paper power-saving). Not confirmed whether it switches the panel VCC rail directly or an EPD PMIC enable. Not a strapping pin on the S3, so no boot hazard.

## Other baseboard GPIOs (DOCUMENTED for E-Series / INFERRED for E1004)
| Function | GPIO | Notes |
|----------|------|-------|
| Onboard green LED | GPIO6 (active-low) | **AMBIGUOUS on E1004** — the E1004 wiki J2 header table also lists GPIO6 as the header "ADC" pin. Verify before using. |
| Buzzer (passive piezo) | GPIO45 | LEDC PWM |
| microSD power enable | GPIO16 (active-high) | drive low to cut SD power in sleep |
| I2C0 (E1002 has SHT4x + PCF8563 RTC) | SDA GPIO19 / SCL GPIO20 | **Presence on E1004 UNKNOWN** |

## Power architecture (INFERRED)
- No soft-power latch / power-enable GPIO that must be held to keep the board alive — the SY6974B power path keeps the system powered from battery or USB; "off" = ESP32-S3 deep sleep. Nothing needs `gpio_hold_en` to stay powered. Consider `gpio_hold` on GPIO12 (panel), GPIO16 (SD), GPIO21 (batt divider) to keep them at a defined off-level through deep sleep.
- No separate fuel-gauge IC documented — battery % is derived from the resistor-divider ADC, not a coulomb counter.

## Sources
- Seeed E1004 wiki: https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/
- Seeed ESPHome cookbook (E-Series buttons/battery/low-power): https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome_advanced/
- Zephyr reTerminal E1002 board (shared baseboard): `boards/seeed/reterminal_e1002/` devicetree + docs
- Silergy SY6974B Zephyr binding
