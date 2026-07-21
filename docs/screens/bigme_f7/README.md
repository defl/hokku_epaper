# Bigme F7 (`bigme_f7`)

The cheapest way into Hokku, and the only supported frame that isn't an ESP32: a
7.3" E Ink Spectra 6 panel (EK79655, 800×480) driven by an **XRADIOTECH XR872AT**.

**Status:** ✅ supported and proven end-to-end on real hardware — custom firmware,
WiFi, image fetch and display, A/B OTA updates, battery reporting, deep sleep, and
mDNS `.local` resolution all work.

Supporting it meant reverse-engineering an SoC with no public SDK support, no
vendor documentation, and no ESP-IDF equivalent — including writing a
[BROM flasher from scratch](bootstrap.md). Most of the documentation in this
folder is the record of that.

**Any F7 variant works.** F7, F7 Lite, F7 Plus, F7 Pro, F7 Max, F7 Ultra, F7 SE
and F7+ all share identical PCB and hardware per Bigme's own FCC equivalence
letter (FCC ID `2A8EM-F7`); only the sales region differs.

## Getting one running

| | |
|---|---|
| **Buy** | [Hardware overview](../../hardware.md#bigme-f7) |
| **Convert to Hokku** | [Bootstrap guide](bootstrap.md) — one-time USB flash |
| **Install the server** | [Appliance image](../../appliance.md) · [manual install](../../install.md) |
| **Use** | [User manual](../../manual.md) |

> ⚠️ **Read [bootstrap.md](bootstrap.md) before buying.** Converting a stock F7
> replaces the vendor firmware via the chip's mask BROM, and is a more involved
> process than flashing the ESP32 frames. [Going back to stock](restore_to_stock.md)
> is documented and tested, but treat this as a one-way trip unless you're
> comfortable with the recovery procedure.
>
> The flashing tooling only ever writes the inactive A/B slot and its config
> sector — the bootloader and the OEM slot are never touched, so a bad image
> rolls back rather than bricking the unit.

## Documentation

**Getting it working**
- [Bootstrap a fresh unit](bootstrap.md) — the mask-BROM catch and first flash
- [Custom firmware](custom_firmware.md) — what our firmware does on this SoC
- [Firmware build](firmware_build.md) — toolchain, SDK patches, building the image
- [OTA updates](ota.md) — the A/B slot scheme and rollback behaviour
- [Restore to stock](restore_to_stock.md) — putting the vendor firmware back

**Hardware**
- [Hardware facts](hardware_facts.md) — confirmed: SoC, flash, panel, ADC, UART
- [Hardware guesses](hardware_guesses.md) — inferences and unknowns
- [Display driver](display_driver.md) — the EK79655 command set and packing
- [EPD init analysis](epd_init_analysis.md) — the panel init sequence, decoded

**Reverse engineering**
- [Overview](reverse_engineering_overview.md) — how the stock firmware was analysed
- [Firmware dump procedure](firmware_dump_procedure.md) — reading the flash out
- [Cloud protocol](cloud_protocol.md) — what the stock firmware talked to

## Panel at a glance

| | |
|---|---|
| Panel | E Ink Spectra 6 (EK79655 controller), 7.3" |
| Resolution | 800 × 480 |
| Colours | Black, white, yellow, red, blue, green |
| SoC | XRADIOTECH XR872AT (Cortex-M4F, 4 MB SPI NOR flash) |
| USB-to-serial | CH340 (VID `1A86:7523`), UART0 @ 115200 |
| WiFi | 2.4 GHz only |
| Recovery | Mask BROM via USB replug + power press |

Despite the "ACeP" wording in some vendor material, this is a **Spectra 6 (E6)**
panel using the same six-ink set and nibble encoding as the 13.3" frames — not a
7-colour ACeP panel. See the note at the top of
[`display.py`](../../../python/hokku/screens/bigme_f7/display.py) for how that was
established.
