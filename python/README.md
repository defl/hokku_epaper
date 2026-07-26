# hokku-server

Spectra 6 six-colour e-ink **image server** for [Hokku](https://github.com/defl/hokku_epaper)
photo frames — the web app, image pipeline (colour-accurate dithering to the
E Ink Spectra 6 palette), and frame-serving API that drives one or more e-paper
frames from a single photo library.

This is the standalone server package. For the full project — supported frames,
firmware, and the zero-terminal Raspberry Pi appliance image — see the
**[main README](https://github.com/defl/hokku_epaper#readme)**.

## Install

```sh
pip install hokku-server
```

`pip` pulls in every runtime dependency automatically (Flask, waitress, Pillow +
HEIF/AVIF/JXL plugins, NumPy, numba, OpenCV, colour-science, resvg, zeroconf,
…). No extra manual install steps are needed for the server to run.

> **The wheel is server-only — it does not bundle screen firmware images.**
> Firmware `.bin`/`.img` files are release build artifacts, not Python data, so
> the "Flash a screen" and over-the-air firmware-update features (which serve a
> bundled firmware image to frames) are unavailable from a bare `pip install`.
> Everything else — uploading photos, the web app, image conversion, and
> serving images to already-flashed frames — works fully. If you need the
> flashing/OTA features, use the Debian package or the appliance image, which
> bundle the firmware. See the
> [installation guide](https://github.com/defl/hokku_epaper/blob/main/docs/install.md).

## Quick start

```sh
# 1. Create a config from the shipped template and edit the paths.
python -c "import importlib.resources as r, shutil; \
shutil.copyfile(r.files('hokku.webserver') / 'config' / 'config.json.example', 'config.json')"
#    Edit config.json: set upload_dir and cache_dir to writable paths.

# 2. Run the server.
hokku-server config.json
```

The server listens on `port` from the config (default `8080`) and serves the web
app at `/hokku/ui`. See the
[user manual](https://github.com/defl/hokku_epaper/blob/main/docs/manual.md) for
day-to-day use.

## License

GPL-3.0 (non-commercial). See the [project repository](https://github.com/defl/hokku_epaper).
