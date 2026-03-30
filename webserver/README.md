# Hokku Image Server

Spectra 6 e-ink image server for the EL133UF1 display. Pre-converts images using Floyd-Steinberg dithering to the 6-color palette.

## Quick start

```bash
pip install flask pillow numpy
mkdir -p /images/upload
cp your-photos/*.jpg /images/upload/
python webserver.py
```

Server starts on `http://0.0.0.0:8080`. Drop images into `/images/upload/` at any time — they are auto-detected and converted. Results are cached in `/images/cache/`.

## Debian/Ubuntu install with systemd

```bash
sudo apt install python3 python3-flask python3-pil python3-numpy
sudo mkdir -p /opt/hokku /images/upload /images/cache
sudo cp webserver.py /opt/hokku/
```

Create `/etc/systemd/system/hokku-server.service`:

```ini
[Unit]
Description=Hokku Spectra 6 image server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/hokku/webserver.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hokku-server
```

## Image processing

Converting images to the Spectra 6 palette takes a while (expect 10-30 seconds per image depending on your hardware). Images are converted in the background when they're first detected in `/images/upload/`, and the results are cached in `/images/cache/`. The server won't serve an image until conversion is complete.

The cache is fully automatic and content-aware. Every 10 seconds (and at startup) the server scans the upload directory and compares SHA-1 hashes of the source files against the cache:

- **Added images** are detected and converted automatically.
- **Changed images** (same filename, different content) trigger a re-conversion and the old cache entry is removed.
- **Removed images** are pruned from both the in-memory pool and the disk cache.

You should only need to manually clear the cache after updating `webserver.py` itself, since changes to the dithering or palette logic won't take effect until the cached files are regenerated.

```bash
curl http://localhost:8080/spectra6/clear_cache
```

## Color correction

The dithering pipeline uses measured palette values instead of theoretical ones. The measured RGB values (`PALETTE_MEASURED_RGB`) represent what the Spectra 6 panel actually renders for each color, which differs significantly from ideal sRGB. These were sourced from the [esp32-photoframe](https://github.com/vroland/esp32-photoframe) project, which photographed a real panel with a colorimeter.

Before dithering, the image undergoes dynamic range compression: the source luminance (L\* 0–100) is remapped to the display's actual range (~1.4–81 L\*). This prevents the dithering algorithm from trying to reproduce brightness levels the panel cannot show, which reduces wasted dither noise in highlights and shadows.

The measured values are from a different Spectra 6 panel and may not perfectly match the EL133UF1. To calibrate for your specific display, photograph a test pattern showing all 6 colors and update `PALETTE_MEASURED_RGB` with the measured sRGB values.

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /spectra6` | 960,000 byte binary (random image) |
| `GET /spectra6/preview` | PNG preview of last served image |
| `GET /spectra6/status` | JSON pool status |
| `GET /spectra6/clear_cache` | Wipe cache and re-convert all |

## ESP32 configuration

Set `IMAGE_URL` in `firmware/main/main.c` to point to your server:

```c
#define IMAGE_URL "http://<server-ip>:8080/spectra6"
```
