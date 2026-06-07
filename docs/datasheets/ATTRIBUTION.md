# Datasheets — Attribution and Download Index

PDF files are **not committed** to this repository (see `.gitignore`).
They are copyrighted third-party documents with no explicit redistribution license.

Run `docs/datasheets/download.sh` to fetch them locally for offline reference.

| Filename | Source URL | Terms | Used by | Notes |
|---|---|---|---|---|
| `EK79655_waveshare_7in3f_application_note.pdf` | https://files.waveshare.com/upload/8/86/7.3inch_e-Paper_(F)_Application_Note_Reference.pdf | Waveshare product doc, no explicit license | bigme_f7 | Application note for 7.3inch e-Paper (F) / Fitipower EK79655 controller. Covers reset sequence, init commands, timing. |
| `XR872AT_datasheet_v1.05.pdf` | https://github.com/XradioTech/xradiotech.github.io/raw/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf | Public doc from XradioTech GitHub, no explicit license | bigme_f7 | XR872AT SoC datasheet v1.05. Memory map, GPIO, peripherals, electrical. |
| `ESP32-S3_datasheet.pdf` | https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf | Espressif public documentation | huessen_epf1301 | ESP32-S3 datasheet. Pin descriptions, electrical characteristics, memory map. |
| `ESP32-S3_technical_reference_manual.pdf` | https://www.espressif.com/sites/default/files/documentation/esp32-s3_technical_reference_manual_en.pdf | Espressif public documentation | huessen_epf1301 | Full TRM (~18 MB). GPIO, SPI, RTC, ADC, deep sleep register details. Large — not downloaded by default; use `download.sh --full`. |
| `UC8179C_datasheet.pdf` | https://raw.githubusercontent.com/CursedHardware/epd-driver-ic/master/UC8179c.pdf | Mirrored by CursedHardware/epd-driver-ic; original from UltraChip (no explicit license) | huessen_epf1301 | UC8179C dual-panel EPD controller datasheet. Init sequence, power, commands. |
| `Zbit_ZB25VQ32_datasheet.pdf` | https://www.zbitsemi.com/upload/file/20201010/20201010174021_57537.pdf | Zbit Semiconductor official site, no explicit license | bigme_f7 | ZB25VQ32 4MB SPI NOR flash (JEDEC 0x5E4016). Used in Bigme F7. |
| `EL133UF1_spec.pdf` | — | Not publicly available | huessen_epf1301 | E Ink EL133UF1 13.3" Spectra 6 panel. No public datasheet; contact E Ink or distributor. Product page: https://www.eink.com/product/detail/EL133UF1 |
