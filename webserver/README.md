# Image Server

Spectra 6 e-ink image server for Hokku/Huessen frames. Converts images using Floyd-Steinberg dithering to the 6-color palette and serves them to connected frames.

## Install

**Debian/Ubuntu** (recommended):
```bash
apt install ./hokku-server_2.1.10-1_all.deb
```
The deb installs dependencies, creates the config, and starts the service automatically.

**From source**:
```bash
pip install flask pillow numpy pillow-heif
python webserver.py
```

## Usage

Drop images into the upload directory and they are automatically converted. The web GUI is available at `http://server:8080/`.

- **Debian install**: images go in `/var/lib/hokku/upload/`, config at `/etc/hokku/config.json`
- **From source**: images go in `/images/upload/`, config at `./config.json`

## Web GUI

The web GUI at `http://server:8080/` lets you:

- **Configure**: timezone, refresh schedule (HHMM format), orientation (landscape/portrait), poll interval
- **Browse images**: thumbnails with original and dithered views, show counts, total display time
- **Upload images**: drag and drop files anywhere on the page, or click the upload zone to browse
- **Pending-dither indicator**: every uploaded image appears in the grid immediately. Files still being converted to the e-ink palette show a yellow "Dithering…" badge and a faded thumbnail; the status bar shows `ready / total` until the batch finishes
- **Delete images**: trashcan button on each thumbnail with a styled confirm dialog (Esc cancels, Enter confirms); also removes the cached conversion
- **Manage screens**: see all connected frames with name, IP, request count, last seen
- **Show Next**: queue a specific image to be shown on the next refresh
- **Clear Cache**: force re-conversion of all images (useful after changing orientation)

## Configuration

Config file (`config.json`):
```json
{
  "timezone": "America/Chicago",
  "refresh_image_at_time": ["0600", "1200", "1800"],
  "upload_dir": "/images/upload",
  "cache_dir": "/images/cache",
  "port": 8080,
  "poll_interval_seconds": 10,
  "orientation": "landscape"
}
```

Config is loaded from (in order): `HOKKU_CONFIG` env var, `./config.json`, `/etc/hokku/config.json`. Changes made via the web GUI are saved back to the same file.

## Image processing

Converting images to the Spectra 6 palette takes 10-30 seconds per image. Images are converted in the background and cached on disk. The cache is content-aware (SHA-1 hashed):

- **Added images** are detected and converted automatically
- **Changed images** (same filename, different content) trigger re-conversion
- **Removed images** are pruned from the cache

## Color correction

The dithering pipeline uses measured palette values from a real Spectra 6 panel (sourced from [esp32-photoframe](https://github.com/vroland/esp32-photoframe)), which differ significantly from theoretical sRGB values. Before dithering, dynamic range compression remaps image luminance to the display's actual L* range (~1.4–81), reducing wasted dither noise in highlights and shadows.

## Fair image distribution

Images are served by `show_index` ranking — lowest index is served next, with random tie-breaking. New images start at index 0 and existing images are leveled to 1, giving new additions priority. The "Show Next" button in the web GUI sets an image's index to min-1.

Total show count and display time are tracked per image in `database.json`.

## Database

The server stores all persistent state in `database.json` in the cache directory. This file is updated automatically and should not normally be edited by hand. It contains:

- **serve_data** — per-image tracking: `show_index` (rotation ranking), `total_show_count`, `total_show_minutes`, `last_request` timestamp
- **screens** — per-screen tracking: `ip`, `request_count`, `last_seen` timestamp

If you delete `database.json`, the server starts fresh — all images get equal priority and screen history is lost. The file is small and safe to back up.

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/hokku/screen/` | GET | 960KB image binary + `X-Sleep-Seconds` header |
| `/hokku/ui` | GET | Web GUI |
| `/hokku/api/status` | GET | JSON status (pool, screens, config, server time) |
| `/hokku/api/original/<name>` | GET | Original uploaded image |
| `/hokku/api/thumbnail/<name>` | GET | 300px thumbnail |
| `/hokku/api/dithered/<name>` | GET | Dithered preview PNG |
| `/hokku/api/show_next/<name>` | POST | Queue image as next |
| `/hokku/api/upload` | POST | Upload one or more images (multipart `files`) |
| `/hokku/api/image/<name>` | DELETE | Delete an uploaded image and its cached conversion |
| `/hokku/api/config` | POST | Update configuration |
| `/hokku/api/clear_cache` | POST | Wipe cache and re-convert |
| `/hokku/api/time` | GET | Current server time in configured timezone |

## Supported image formats

JPEG, PNG, BMP, TIFF, WebP, GIF, HEIC/HEIF, and AVIF.

## Systemd service

The deb package installs a systemd service. Useful commands:

```bash
systemctl status hokku-server    # check status
systemctl restart hokku-server   # restart after config changes
journalctl -u hokku-server -f    # follow logs
```
