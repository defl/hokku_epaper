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

If you add or remove photos from the upload directory, hit the clear cache endpoint to force a full re-scan and re-convert. You'll also want to clear the cache after updating `webserver.py` itself, since any changes to the dithering or palette logic won't take effect until the cached files are regenerated.

```bash
curl http://localhost:8080/spectra6/clear_cache
```

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
