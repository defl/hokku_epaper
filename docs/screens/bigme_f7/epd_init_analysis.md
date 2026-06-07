# Bigme F7 — EK79655 Init Sequence Analysis

Compares the EK79655 spec, Waveshare reference driver, disassembled Bigme F7 OEM firmware,
and our custom firmware (`firmware_bigme_f7/epd.c`).

Sources:
- **App note**: Waveshare 7.3inch e-Paper (F) Application Note Reference (Fitipower EK79655)
- **Waveshare driver**: `EPD_7in3f.c` from github.com/waveshareteam/e-Paper
- **Bigme F7 OEM**: disassembly of `01_boot_payload.bin` — functions `RST_pulse` (0x002071E8),
  `EPD_step2` (0x00207228), `send_image_data` (0x0020736C)
- **Our firmware**: `firmware_bigme_f7/epd.c`

## Reset Sequence

| | App Note | Waveshare Driver | Bigme F7 OEM | Our firmware |
|---|---|---|---|---|
| Pulse type | Double | Single | Double | Double ✓ |
| RST active level | LOW | LOW | LOW | LOW ✓ |
| RST LOW duration | 30 ms | 2 ms | 100 ms | 100 ms ✓ |
| RST HIGH between pulses | 30 ms | — (single pulse) | 100 ms | 100 ms ✓ |
| BUSY wait after reset | Not specified | Yes | Yes | Yes ✓ |
| Delay after BUSY goes high | Not specified | **30 ms** | None | None |

The app note and Waveshare driver contradict each other (double vs single pulse, 30ms vs 2ms).
Bigme OEM follows the app note's double-pulse pattern with more conservative 100ms timing.
Our firmware matches the OEM exactly.

The 30ms post-BUSY delay present in the Waveshare driver is absent in both OEM and our
firmware. OEM works on real hardware without it, so this is intentional. If the display
proves unreliable on first boot, add `OS_MSleep(30)` after `epd_wait_busy()` in
`epd_run_init_sequence()` as the first thing to try.

## Init Command Sequence

| Cmd | Waveshare Driver | Bigme F7 OEM | Our firmware | Notes |
|---|---|---|---|---|
| `0xAA` CMDH | `49 55 20 08 09 18` | `49 55 20 08 09 18` | `49 55 20 08 09 18` | ✓ match |
| `0x01` PWR | `3F 00 32 2A 0E 2A` (6 bytes) | `3F` (1 byte) | `3F` | OEM uses 1 byte; Waveshare uses 6 |
| `0x00` PSR | `5F 69` | `5F 69` | `5F 69` | ✓ match |
| `0x05` POFS | `40 1F 1F 2C` | `40 1F 1F 2C` | `40 1F 1F 2C` | ✓ match |
| `0x08` BTST1 | `6F 1F 1F 22` | `6F 1F 1F 22` | `6F 1F 1F 22` | ✓ match |
| `0x06` BTST2 | `6F 1F 1F 22` | `6F 1F 17 17` | `6F 1F 17 17` | last 2 bytes differ from Waveshare |
| `0x03` BTST_N | not sent | `00 54 00 44` | `00 54 00 44` | OEM-only command |
| `0x13` IPC | `00 04` | not sent | not sent | Waveshare-only |
| `0x60` TCON | `02 00` | `02 00` | `02 00` | ✓ match |
| `0x30` PLL | `3C` | `08` | `08` | different frame rate setting |
| `0x41` TSE | `00` | not sent | not sent | Waveshare-only |
| `0x50` CDI | `3F` | `3F` | `3F` | ✓ match |
| `0x61` TRES | `03 20 01 E0` | `03 20 01 E0` | `03 20 01 E0` | ✓ match (800×480) |
| `0x82` VCOM | `1E` | not sent | not sent | Waveshare-only |
| `0x84` vendor | `00` | `01` | `01` | value differs |
| `0x86` AGID | `00` | not sent | not sent | Waveshare-only |
| `0xE3` PWS | `2F` | `2F` | `2F` | ✓ match |
| `0xE0` CCSET | `00` | not sent | not sent | Waveshare-only |
| `0xE6` TSSET | `00` | not sent | not sent | Waveshare-only |

## Refresh / Update Sequence

| Step | App Note | Waveshare Driver | Bigme F7 OEM | Our firmware |
|---|---|---|---|---|
| DTM `0x10` + 192000 bytes | first | first | first | first ✓ |
| PON `0x04` | ✓ | ✓ | ✓ | ✓ |
| BUSY wait after PON | ✓ | ✓ | ✓ | ✓ |
| BTST `0x06` `6F 1F 17 49` | not mentioned | not mentioned | **yes** | **yes** ✓ |
| DRF `0x12` `00` | ✓ | ✓ | ✓ | ✓ |
| BUSY wait after DRF | ✓ (×2 in spec) | ✓ (×1) | ✓ (×1) | ✓ (×1) |
| POF `0x02` `00` | ✓ | ✓ | ✓ | ✓ |
| BUSY wait after POF | ✓ | ✓ | ✓ | ✓ |

The BTST re-send before DRF (with different params `6F 1F 17 49` vs init-time `6F 1F 17 17`)
is unique to the Bigme F7 OEM — not in the spec or Waveshare driver. It appears to be a
booster pre-conditioning step the manufacturer added for their specific panel.

## Verdict

Our firmware is a faithful match to the Bigme F7 OEM. Every command, parameter, the double
RST pulse, and the BTST re-send before DRF all match the disassembly. The deviations from
the Waveshare reference driver are the Bigme manufacturer's deliberate tuning choices, not
bugs.
