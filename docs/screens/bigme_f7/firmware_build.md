# Bigme F7 Custom Firmware — Build Notes

## Build system

Source lives in `firmware_bigme_f7/` (outside the SDK tree).
SDK: `divadiow/xr872_sdk` cloned alongside at `../xr872_sdk`.

Build requires Docker (ubuntu:18.04 / GCC 6.3.1):

```bash
docker run --rm \
  -v "$(pwd):/hokku_epaper" \
  -v "/path/to/xr872_sdk:/xr872_sdk" \
  ubuntu:18.04 bash -c "
    dpkg --add-architecture i386 &&
    apt-get update -qq &&
    apt-get install -y -q gcc-arm-none-eabi binutils-arm-none-eabi make lib32z1 lib32stdc++6 &&
    chmod +x /xr872_sdk/tools/mkimage &&
    cd /hokku_epaper/firmware_bigme_f7/gcc &&
    make image XR872_SDK=/xr872_sdk CC_DIR=/usr/bin
  "
```

- `make` / `make all` — compile only (produces .axf + .bin + _xip.bin)
- `make image` — compile + assemble flashable `xr_system.img`

Why ubuntu:18.04 / GCC 6.3.1: the SDK redefines `__INT32_TYPE__` as `int`
via `__CONFIG_LIBC_REDEFINE_GCC_INT32_TYPE` to fix `int32_t` vs `int`
conflicts. GCC 10+ stdint.h rejects this with "conflicting types for
int32_t". GCC 6 accepts it.

Why lib32: `xr872_sdk/tools/mkimage` is a 32-bit Linux ELF. Requires
`lib32z1` and `lib32stdc++6` to run in a 64-bit container.

## Output files

After `make image`:

```
firmware_bigme_f7/gcc/hokku_bigme_f7.axf      ~600 KB   ELF (no debug)
firmware_bigme_f7/gcc/hokku_bigme_f7.bin       21 KB    SRAM portion
firmware_bigme_f7/gcc/hokku_bigme_f7_xip.bin  464 KB    XIP (flash-execute) portion
firmware_bigme_f7/image/xr872/xr_system.img     1 MB    flashable image
```

## Flash layout and why the image is always ~1 MB

The `xr_system.img` spans flash offsets 0 to ~1019 KB regardless of app size:

| Offset  | Section        | Size  | Source          |
|---------|----------------|-------|-----------------|
| 0 K     | boot_40M.bin   | 10 KB | SDK binary      |
| 32 K    | app.bin        | 21 KB | SRAM code       |
| 76 K    | app_xip.bin   | 464 KB | XIP code + libs |
| 540 K   | (gap)          | 440 KB | OTA update reserve |
| 980 K   | wlan_bl.bin    | 2 KB  | SDK binary      |
| 984 K   | wlan_fw.bin    | 33 KB | SDK binary      |
| 1018 K  | wlan_sdd_40M   | 1 KB  | SDK binary      |

The image is ~1 MB because the WLAN section is fixed at offset 980 K by
the XR872 system architecture. The 440 KB gap between app_xip and wlan_bl
is OTA update space and is not stored in the image file — mkimage writes
the actual binary at each section's flash offset, so the file must span
the full range.

## Optimization — already at minimum

The SDK's `gcc.mk` already applies all standard Cortex-M size-reduction
flags:

- `-Os` (optimize for size)
- `-ffunction-sections -fdata-sections` + `-Wl,--gc-sections` (dead code elimination)
- `--specs=nano.specs` (newlib-nano, smallest libc)
- `-fomit-frame-pointer`

The 464 KB XIP is dominated by the precompiled WiFi/TLS/TCP-IP stack
(WPA supplicant, lwIP, mbed TLS, HTTP client). These are pulled in
transitively by `platform_init()` and the HTTP client calls. LTO does
not help because the libraries are precompiled without LTO bytecode.
There is no further size reduction available without removing features.

Debug info is stripped from compilation (`DBG_FLAG :=` in the Makefile)
to speed up builds. DWARF sections are stripped by objcopy when producing
`.bin` files anyway, so this does not affect the flashable output.

## Chip configuration (localconfig.mk)

```makefile
export __CONFIG_CHIP_TYPE := xr872
export __CONFIG_HOSC_TYPE := 40    # 40 MHz crystal (confirmed from hardware)
export __CONFIG_XIP := y           # execute-in-place from flash
```

## Known SDK quirks

- `platform_init.c` `#include "command.h"` — must be provided by each
  project. Our `command.h` declares `void main_cmd_exec(char *cmd)`.
- `UINT32 = unsigned long` in SDK vs `uint32_t = unsigned int` in GCC 6 —
  incompatible pointer types under `-Werror`. All HTTP client length/count
  variables must be declared as `UINT32`, not `uint32_t`.
- `IMAGE_CFG_PATH` and `IMAGE_TOOL` in `project.mk` are built as
  `../$(ROOT_PATH)/...` (relative to the image dir), which breaks when
  `ROOT_PATH` is an absolute path. Both are overridden in the Makefile.
