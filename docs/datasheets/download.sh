#!/usr/bin/env bash
# Download third-party datasheets for offline reference.
# Files are not committed — see ATTRIBUTION.md for sources and copyright notes.
#
# Usage:
#   ./download.sh          download standard set (skips large TRM)
#   ./download.sh --full   also download ESP32-S3 TRM (~18 MB)
set -e
cd "$(dirname "$0")"

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

dl() {
    local file="$1" url="$2"
    if [ -f "$file" ]; then
        echo "  already exists: $file"
    else
        echo "  downloading: $file"
        curl -fsSL -o "$file" "$url"
    fi
}

# bigme_f7
dl "EK79655_waveshare_7in3f_application_note.pdf" \
   "https://files.waveshare.com/upload/8/86/7.3inch_e-Paper_(F)_Application_Note_Reference.pdf"

dl "XR872AT_datasheet_v1.05.pdf" \
   "https://github.com/XradioTech/xradiotech.github.io/raw/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf"

dl "Zbit_ZB25VQ32_datasheet.pdf" \
   "https://www.zbitsemi.com/upload/file/20201010/20201010174021_57537.pdf"

# huessen_epf1301
dl "ESP32-S3_datasheet.pdf" \
   "https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf"

dl "UC8179C_datasheet.pdf" \
   "https://raw.githubusercontent.com/CursedHardware/epd-driver-ic/master/UC8179c.pdf"

# Large files — only with --full
if [ "$FULL" = "1" ]; then
    dl "ESP32-S3_technical_reference_manual.pdf" \
       "https://www.espressif.com/sites/default/files/documentation/esp32-s3_technical_reference_manual_en.pdf"
else
    echo "  skipped: ESP32-S3_technical_reference_manual.pdf (~18 MB) — run with --full to include"
fi

# ED2208-NCA_(EL133UF1)_Simple_Spec_v1_20240620.pdf — request via E Ink contact form at
# https://www.eink.com/product/detail/EL133UF1 — no direct download URL available

echo "done."
