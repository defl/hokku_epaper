#!/usr/bin/env bash
# Download third-party datasheets for offline reference.
# Files are not committed — see ATTRIBUTION.md for sources and copyright notes.
set -e
cd "$(dirname "$0")"

dl() {
    local file="$1" url="$2"
    if [ -f "$file" ]; then
        echo "  already exists: $file"
    else
        echo "  downloading: $file"
        curl -fsSL -o "$file" "$url"
    fi
}

dl "EK79655_waveshare_7in3f_application_note.pdf" \
   "https://files.waveshare.com/upload/8/86/7.3inch_e-Paper_(F)_Application_Note_Reference.pdf"

dl "XR872AT_datasheet_v1.05.pdf" \
   "https://github.com/XradioTech/xradiotech.github.io/raw/master/docs/doc/XR872/XR872_Datasheet_V1.05.pdf"

echo "done."
