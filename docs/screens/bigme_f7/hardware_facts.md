# Bigme F7 — Hardware Facts

Only confirmed information lives here. Inferences, estimates, and TBD items belong in [`hardware_guesses.md`](hardware_guesses.md).

## Product Variants

All of the following share **identical PCB and hardware** (source: FCC equivalence letter 2025-08-21, signed by Bigme Cloud Literacy Technology Co., Ltd.):
F7, F7 Lite, F7 Plus, F7 Pro, F7 Max, F7 Ultra, F7 SE, F7+
Only the sales region differs.

FCC ID: **2A8EM-F7**
Manufacturer: Bigme Cloud Literacy Technology Co., Ltd. / xrztech.com

## SoC

- **Part**: XRADIOTECH XR872AT
- **Source**: Chip marking visible in FCC internal photo 12: `XRADIOTECH / XR872AT / N6135EA / 68F1`
- **Package**: QFN52, 6×6 mm (from datasheet)
- **CPU**: ARM Cortex-M4F (from datasheet)
- **Max clock**: 384 MHz (from datasheet)
- **SRAM**: 416 KB (from datasheet)
- **Boot ROM**: 160 KB (from datasheet)
- **WiFi**: 2.4 GHz 802.11b/g/n, integrated (from datasheet)
- **NOT an ESP32** — esptool does not work; see [`hardware_guesses.md`](hardware_guesses.md) for tooling
- **Crystal**: 40 MHz (confirmed from boot log: `HF clock 40000000 Hz`)
- **CPU clock in use**: 240 MHz (confirmed from boot log: `cpu clock 240000000 Hz`; max is 384 MHz per datasheet)
- **XIP**: enabled (confirmed from boot log: `XIP: enable`)
- **SRAM heap available**: 332,156 bytes at [0x21ca84, 0x26dc00) (confirmed from boot log)

## Firmware

Source: UART boot log captured 2026-06-06, full log at `.private/uart_log_20260606_104615.txt`
Binary analysis of flash dump performed 2026-06-06; partition files in `.private/units/<serial>_<tag>/partitions/`.

- **SDK**: XRADIO Skylark SDK 1.2.3
- **App firmware version**: **1.2.7** (string literal `1.2.7` at boot payload offset 0x84B1)
- **Build date**: Aug 7 2025 15:44:24
- **WLAN firmware version**: R-XR_C10.08.52.64_01.80 (built Jul 6 2019)
- **WLAN driver version**: XR_V02.06.28
- **Flash chip**: Zbit Semiconductor SPI NOR, **4 MB**, JEDEC ID `0x5E4016`
- **Flash dump**: `.private/units/<serial>_<tag>/flash_full.bin` (4,194,304 bytes, `AWIH` magic, captured 2026-06-06). Dumps are organised per physical unit under `units/<serial>_<tag>/`; see each unit's `NOTES.md`.
- **BROM version**: 2 (confirmed via PhoenixMC flash ID dialog)
- **Dump procedure**: [`firmware_dump_procedure.md`](firmware_dump_procedure.md)
- **MAC address (efuse)**: 18:9e:2d:f9:87:54

## Flash Partition Layout

Confirmed from AWIH header analysis of flash dump. Each partition image has its own 64-byte AWIH header.

| Flash offset | Size | Name | Load | Payload size |
|---|---|---|---|---|
| 0x000000 | 32 KB | Main image table | — | 10 KB (partition table) |
| 0x008000 | 128 KB | Boot / app | SRAM 0x00201000, EP 0x00201100 | 48 KB |
| 0x028000 | 819 KB | App XIP | XIP (runs from flash) | 757 KB |
| 0x0F4C00 | 4 KB | WLAN bootloader | XIP | 2 KB |
| 0x0F5C00 | 35 KB | WLAN firmware | XIP | 34 KB |
| 0x0FE800 | ~3 MB | WLAN SDD data | — | 1 KB |

The EPD display driver lives in the **Boot partition** (SRAM-loaded). Confirmed EPD functions found:
`check_busy_high`, `epd_test` (with sub-steps 1, 11, 2, 3).

## XIP / Flash Cache Mapping

Source: SDK ROM disassembly (`xr872_sdk/src/rom/rom_bin/out/rom_source_0424.objdump`,
`rom_HAL_Flashc_Xip_Init` at ROM 0xb0d0) cross-checked against the OEM flash dump, 2026-06-13.

- XIP code executes from VMA base **0x400000**. The flash instruction cache maps that VMA
  window to flash via the **OPI memory controller** at base **`0x4000B000`**:
  - `OPI_MEM_CTRL->START_ADDR0` = `0x4000B080` — start of cached VMA window
  - `OPI_MEM_CTRL->END_ADDR0`   = `0x4000B084` — end of cached VMA window
  - `OPI_MEM_CTRL->BIAS_ADDR0`  = **`0x4000B088`** — bits[27:0] = flash byte offset where the
    XIP image data begins; bit31 = bias enable
- Mapping: `flash_addr = (VMA - 0x400000) + BIAS_ADDR0[27:0]`.
- The arch-v1 `FLASH_CACHE` block (base `0x4000C000`, `READ_BIAS_ADDR` at `0x4000C098`) from
  the SDK headers **does NOT exist / is unused on this silicon** — zero references in the OEM
  firmware. XR872AT uses the arch-v2 OPI flash controller above.
- Programmed at boot by `platform_init_level0` → `platform_xip_init()` →
  `HAL_Xip_Init(flash, app_xip_data_offset)` (which sets `BIAS_ADDR0 = offset`), run from SRAM
  before `platform_init_level1` (the first code that executes from XIP).
- OEM `app_xip` data is at flash **0x28040** (header 0x28000), so OEM boots with
  `BIAS_ADDR0 = 0x28040`.

## BROM / Recovery

Source: direct testing on the device, 2026-06-13.

- **Only software BROM trigger is the firmware console `upgrade` command** (`cmd_upgrade_exec`
  → sets `PRCM CPUA_BOOT_FLAG = SYS_UPDATE` → watchdog reboot → bootloader enters `bl_upgrade`
  and waits in BROM). If running firmware crashes before its console starts, this path is gone.
- **DTR and RTS are NOT wired** to the XR872 reset or power-enable: pulsing either line (both
  polarities) with a 9 s listen produces no boot/crash output. PhoenixMC's DTR-reset is a no-op
  on this board.
- **The power-on mask-BROM UART sync window IS reachable over the CH340 — with the right trigger.**
  Proven 2026-07-07 (`tools/_catch_test.py`; used by `tools/f7_initial_flasher.py`). The trigger is
  a **USB replug + power press**, NOT a long-press:
  - **Replug + press (works):** unplug the cable, replug it, *then* press power. The replug brings
    the CH340 port up **first**, so a Python watcher holds it open and hammers `0x55` straight
    through the sync window — no re-enumeration gap.
  - **Long-press (fails):** a long-press power-cycles the CH340 too, so the port disappears ~1 s and
    only re-opens ~1.1 s after power-on — by then the BROM window has closed and the catch misses.
    Our earlier "unreachable over the CH340" conclusion was this wrong trigger.
  PhoenixMC connects the same way (its own driver timing) once the device is in the window.
- **Implication — the "bricked" unit may be recoverable.** The mask-BROM's sync window runs at
  power-on, **before** the app boots and crashes, so the replug+press catch should land on a
  crash-on-boot device too. This is UNTESTED on the bricked unit (6000203) but is worth trying
  before resorting to hardware (SPI-clip the NOR flash / boot-strap test pad).
- **On a WORKING device, the `upgrade` console is a reliable UART BROM entry** (confirmed 2026-06-19
  on a working unit): send `upgrade\n` to the awake firmware console → watchdog reset into BROM. No
  boot-log-window timing is needed; the CH340 stays powered so the COM port does not bounce, and the
  device auto-reboots back to the console after each BROM watchdog reset, so a tool can re-enter BROM
  repeatedly with no button presses. `tools/_dump_bigme_f7.py` uses this to dump 4 MB fully
  automatically.

### BROM command wire protocol

Source: disassembly of the phoenixMC Linux ELF (`CFlashHost::*`, symbols intact) cross-checked
against live transactions, 2026-06-19. Implemented in `python/hokku/common/xr872/flasher.py`.

- Sync: host sends `0x55` until BROM replies `OK`. Commands are 12-byte-header frames:
  `"BROM"(4) + type(1)=0x04 + pad(1) + CRC16_LE(2) + count_BE(4) + cmd(1) + payload_BE`.
- **ReadSector (0x1A) and WriteSector (0x1B) address flash in 512-byte SECTOR units, not bytes:**
  the on-wire address field is a **sector index (`byte_addr >> 9`)** and the length field a
  **sector count (`bytes >> 9`)**, both big-endian. A read/write of byte address A for N bytes puts
  `A>>9` and `N>>9` on the wire. *(Passing raw byte values silently reads/writes 512× too far —
  this was the cause of earlier corrupt UART dumps; fixed and then verified byte-for-byte identical
  to a PhoenixMC dump, and by a write→readback→erase round-trip on erased regions.)*
- WriteSector streams a fixed **16 KB (0x20 sectors) data block per frame** after its header ACK;
  each block has its own ACK. ReadSector streams `sector_count × 512` bytes after the ACK.
- **EraseFlash (0x19) is the exception — it uses RAW BYTE addressing**, erase-type byte `0x03`,
  one erase per **64 KB block** (64 KB-aligned). Do not sector-convert erase addresses.
- ChangeBaud to 921600 is rejected in practice — the CH340 corrupts bulk data above 115200.

## Cloud Connectivity

Source: string literals extracted from boot partition binary, 2026-06-06. Full protocol in [`cloud_protocol.md`](cloud_protocol.md).

- **Cloud server**: `http://ereader.bigme.vip:8086`
- **MQTT broker**: `120.76.40.178:1883`
- **MQTT credentials**: user `mqt_user`, password `xrz86112763`
- **MQTT topics**: `iot/device/<deviceId>`, `iot/device/willTopic`, `iot/device/sever_topic`
- **Device ID format**: `BIGME_<MAC>` (e.g. `BIGME_189E2DF98754`)
- **Setup AP SSID**: `BigmeFrameRouter`, password `88888888`
- **Operational AP SSID**: `XRZ_<MAC>` (used during WiFi provisioning)
- **Default timezone**: UTC+8 (`TZ=GMT-8` is the POSIX convention for Asia/Shanghai)

## Antenna

PCB trace antenna, labeled "Antenna" with arrow in FCC internal photo 12.

## PCB

- **Shape**: L-shaped, wraps along bottom and one side of the display (FCC photos 10/11)
- **Display connector**: Wide FPC/ZIF connector at the bottom edge (FCC photos 10/11)
- **Button**: Single power button on the bottom of the frame (product description + FCC external photos)
- **USB-to-serial**: CH340 bridge (USB VID 1A86:7523), exposes XR872AT UART0 at 115200 baud (confirmed 2026-06-06)

## Battery

Source: label visible in FCC internal photo 9.

- **Model**: U255671P (Li-ion)
- **Rated capacity**: 1300 mAh
- **Rated energy**: 4.81 Wh
- **Nominal voltage**: 3.7 V
- **Charge voltage limit**: 4.2 V
- **Standard**: GB 31241-2022
- **Manufacturer**: Shenzhen Utility Energy Co., Ltd.
- **Date on unit**: 2025-05-26

### Voltage sensing (confirmed 2026-07-06, reverse-engineered from OEM firmware)

The pack voltage is read on **ADC channel 4 = pin PA14** through an external
resistor divider — **NOT** `ADC_CHANNEL_VBAT` (channel 8), which measures the SoC's
regulated internal rail (a steady ~2.58 V that is meaningless as a battery gauge).

The OEM's `adc_voltage_get()` (disassembled at VMA `0x20122c` in the boot
partition) reads channel 4 and scales the raw 12-bit value (2500 mV ref, ratio-1
channel behind a ~4.37:1 divider) as:

```
mv = raw * 295000 / 1105920 * 10      (≈ raw × 2.6674),  clamped to 4200 mV
```

Our firmware's `hokku_battery_mv()` uses exactly this. `HAL_ADC_Conv_Polling(
ADC_CHANNEL_4, …)` auto-configures the PA14→CH4 pinmux via the `xr872_evb_ai`
board config. Verified on-device: reads ~4170–4200 mV / 100 % on USB.

## Display

Source: product description + E Ink Spectra 6 spec + disassembly of boot partition (2026-06-06).
Full driver analysis: [`display_driver.md`](display_driver.md)

- **Technology**: E Ink Spectra 6 (ACeP)
- **Size**: 7.3 inch
- **Resolution**: 800×480 pixels (confirmed by TRES command 0x61 params 0x0320, 0x01E0 in firmware)
- **PPI**: 127.8
- **Colors**: 7 (black, white, red, green, blue, yellow, orange)
- **No backlight**
- **Controller**: EK79655 or direct compatible — initialization sequence is byte-for-byte identical to
  Waveshare EPD_7in3f open source driver; confirmed from boot partition disassembly
- **Image format**: 192,000 bytes raw, 4 bits per pixel, 2 pixels per byte (upper nibble = left pixel)
  Color encoding: 0=Black, 1=White, 2=Green, 3=Blue, 4=Red, 5=Yellow, 6=Orange
- **SPI interface**: bit-banged, 9-bit frames (1 D/C bit + 8 data bits), MSB first
  PA19=MOSI/DC, PA21=SCLK, PA22=CS (active low), PA9=BUSY input (HIGH = ready)
- **Refresh time**: ~20–30 seconds for full ACeP refresh

## SDK Ecosystem

The device firmware was built with **XRADIO Skylark SDK 1.2.3** (Aug 7 2025). Research conducted 2026-06-06 found no public availability of this version anywhere — making it the newest confirmed SDK version and suggesting the manufacturer has a private/NDA channel with Allwinner.

Publicly available SDK versions (for reference):

| Version | Source | Notes |
|---|---|---|
| 1.1.1 | github.com/XradioTech/xradio-skylark-sdk | Last official public release (Jun 2020, abandoned) |
| 1.2.0 | github.com/divadiow/xr872_sdk | Community fork; also seen in device boot logs (Apr 2023 / Jun 2024 builds) |
| 1.2.2 | github.com/divadiow/xr872_sdk | Community fork ceiling (Feb 2023); referenced as "test build" in Elektroda threads |
| **1.2.3** | **This device only** | **No public source found; built Aug 7 2025** |

Allwinner (who acquired XRadioTech) does not publicly host an XR872 SDK. Their official developer platform (aw-ol.com / docs.aw-ol.com) lists XR806 but not XR872. The community fork at github.com/divadiow/xr872_sdk (active through May 2025) is the best public alternative.

## Key References

- XR872AT Datasheet v1.05: https://github.com/XradioTech/xradiotech.github.io/blob/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf
- Official XR872 SDK (abandoned at 1.1.1): https://github.com/XradioTech/xradio-skylark-sdk
- Community SDK fork (1.2.2, active): https://github.com/divadiow/xr872_sdk
- Allwinner developer platform: https://www.aw-ol.com/
- FCC filing: https://fccid.io/2A8EM-F7
