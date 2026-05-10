# Installation

## What you need

**A computer to run the server on.** This can be anything on your local network — a Raspberry Pi, a spare laptop, a NAS, a desktop that's always on. A Raspberry Pi Zero 2 W is the most popular choice because it's cheap, silent, uses almost no power, and is more than fast enough. The server needs to be reachable by the frame at all times, so something that stays on makes more sense than a laptop you close.

**The frame.** The Hokku / Huessen 13.3" six-colour e-ink display. The board inside is an ESP32-S3 — the pre-built firmware is matched to it, you don't need to worry about the hardware details.

**A data-capable USB-C cable** for the initial setup. Not all USB-C cables carry data — charge-only cables are common and won't work. If nothing shows up when you plug in, try a different cable.

**2.4 GHz WiFi.** The frame's chip doesn't support 5 GHz. Make sure the network you want to use is 2.4 GHz (most routers broadcast both and you can use either SSID).

---

> **All commands in this guide assume you are in the project root directory** — the folder that contains `hokku_setup.bat`, `requirements.txt`, and the `tools/` and `webserver/` subdirectories. Open your terminal there before running anything.

## Contents

1. [Manual installation](#1-manual-installation) — no scripts, full control, any platform
   - [1.1 Install the image server](#11-install-the-image-server)
   - [1.2 Set up a Raspberry Pi (optional)](#12-set-up-a-raspberry-pi-optional)
   - [1.3 Flash and configure the frame](#13-flash-and-configure-the-frame)
2. [Using the setup wizard](#2-using-the-setup-wizard) — guided, downloads everything, Windows + Pi in one run
   - [2.1 Prerequisites](#21-prerequisites)
   - [2.2 Running the wizard](#22-running-the-wizard)
   - [2.3 What the wizard does, step by step](#23-what-the-wizard-does-step-by-step)
3. [Troubleshooting](#3-troubleshooting)

---

## 1. Manual Installation

You need two things: the **image server** running on a computer on your network, and the **firmware** flashed onto the frame over USB.

### 1.1 Install the image server

**Debian / Ubuntu (recommended)**

Download the `.deb` from the latest GitHub release, then:

```bash
apt install ./hokku-server_2.1.20-1_all.deb
```

The package installs a systemd service that starts automatically on boot. The web GUI will be at `http://<your-server>:8080/`. Photos go into `/var/lib/hokku/upload/` — use the web uploader, or install Samba so you can drop files in from any machine on your network.

Useful service commands:

```bash
systemctl status hokku-server     # check it's running
systemctl restart hokku-server    # restart after manual config edits
journalctl -u hokku-server -f     # follow the logs
```

**Configuration**

The server reads its config from (in order): the `HOKKU_CONFIG` environment variable, `./config.json`, `/var/lib/hokku/config.json`, `/etc/hokku/config.json`. Changes saved via the web app are written back to whichever file was loaded. Timezone follows the host OS — set it with `sudo timedatectl set-timezone <IANA>` on the Pi.

A minimal config looks like this:

```json
{
  "refresh_image_at_time": ["0600", "1200", "1800"],
  "upload_dir": "/var/lib/hokku/upload",
  "cache_dir": "/var/lib/hokku/cache",
  "port": 8080,
  "orientation": "landscape"
}
```

All options can also be changed live from the web app without restarting the server.

**From source (any platform)**

Create a virtual environment, install dependencies, and start the server:

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cd webserver
python -m webserver
```

The `cd webserver` step is required — the server package lives there. Photos go in `webserver/images/upload/`. Web GUI at `http://<your-server>:8080/`.

---

### 1.2 Set up a Raspberry Pi (optional)

Skip this section if you're running the server on an existing machine. The setup wizard handles SD card imaging automatically on Windows — this section is for everyone else.

Use the official [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and configure these settings before writing:

- **OS:** Raspberry Pi OS Lite 64-bit (no desktop needed)
- **Hostname:** `hokku-server` (so it appears as `hokku-server.local` on the network)
- **WiFi:** your SSID and password, plus the correct country code
- **SSH:** enabled — strongly recommended, without it there's no way to check logs remotely
- **User:** create a user (e.g. `hokku` / `hokku`)
- **Timezone:** your local timezone

Once the Pi boots and you can SSH in, install the `.deb`:

```bash
scp hokku-server_*.deb hokku@hokku-server.local:~
ssh hokku@hokku-server.local
sudo apt install ./hokku-server_*.deb
```

**First boot is slow.** On a Pi Zero 2 W expect 3–8 minutes for the OS to fully initialise, install packages, and bring the webserver up. If you're waiting for `http://hokku-server.local:8080/` to respond, be patient. SSH in and tail `/var/log/hokku-firstboot-install.log` if you want to watch progress.

---

### 1.3 Flash and configure the frame

**Prerequisites**

- A data-capable USB-C cable — not all cables carry data, try a different one if nothing shows up
- A virtual environment with dependencies installed (if you haven't already):

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

**Step 1: Connect and identify the port**

Connect the frame to your computer via the USB-C charging port. Find the serial port:

- **Windows:** Device Manager → Ports (COM & LPT) — look for a Silicon Labs or CP210x device and note the COM number (e.g. `COM3`). If it doesn't appear, install the [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers).
- **Linux / macOS:** `ls /dev/ttyUSB* /dev/ttyACM* /dev/cu.usbserial*` — the frame typically appears as `/dev/ttyUSB0` or similar.

**Step 2: Flash the firmware**

Download `hokku-firmware_<tag>.bin` from the latest GitHub release, then:

```bash
esptool.py --chip esp32s3 --port <PORT> write_flash 0x0 hokku-firmware_<tag>.bin
```

The flash takes about 30 seconds.

**Step 3: Write the configuration**

The frame reads its configuration from NVS (non-volatile storage), written over USB. These are the values you need to supply:

| Field | Notes |
|---|---|
| WiFi SSID | 1–32 bytes, no quotes, backslashes, or newlines |
| WiFi Password | 8–63 characters, or empty for an open network |
| Server IP | The LAN IP of the machine running the image server |
| Server Port | `8080` (default) |
| Screen Name | Optional, up to 64 bytes UTF-8, e.g. `Living Room` |

The setup tool can handle just the config-write step if you prefer not to do it by hand:

```bash
python tools/hokku_setup.py
# Select: [4] ESP32: configure only (keep existing firmware)
```

**Step 4: Verify**

After rebooting with valid config the frame connects to WiFi, fetches its first image from the server, and settles into its refresh schedule. If something is wrong it renders a readable error message directly on the e-paper — no serial cable needed.

---

## 2. Using the Setup Wizard

The setup wizard (`hokku_setup.py` / `hokku_setup.bat`) is an interactive menu-driven tool that handles the full install in one run: imaging the Pi OS SD card, downloading firmware, and configuring and flashing the frame. It saves your settings between runs so you don't have to re-enter WiFi credentials every time.

On Windows, double-clicking `hokku_setup.bat` is enough — it creates a `.venv` in the project root, installs dependencies, and launches the wizard with the necessary privileges automatically.

### 2.1 Prerequisites

> If you followed the manual installation path in section 1 you already have these. Skip to [section 2.2](#22-running-the-wizard).

- Python 3.9+
- A virtual environment with dependencies (the `.bat` file does this for you on Windows):

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

- A data-capable USB-C cable connecting the frame to your computer

### 2.2 Running the wizard

```bash
python tools/hokku_setup.py
```

The wizard scans for a connected frame on startup and displays its current state:

```
  Connected frame
  ---------------
  Port:      COM3
  Firmware:  20260421035048Z  (up to date)
  Config:    WiFi=MyNetwork, server=192.168.1.10:8080, name=Living Room
```

It then presents a menu and pre-selects the most sensible option for the detected state:

```
  What would you like to do?
    [1] Full install — image SD card, then configure + flash ESP32
    [2] Server only — image SD card with hokku-server, skip ESP32
    [3] ESP32: configure + flash firmware  <-- default
    [4] ESP32: configure only (keep existing firmware)
    [5] ESP32: flash firmware only (keep existing config)
    [6] Advanced — install settings, cache management
    [7] Exit
```

### 2.3 What the wizard does, step by step

**Full install [1] and Server only [2] — Pi OS SD card imaging (Windows only)**

> These options require Windows and administrator privileges. They write directly to the raw disk.

1. **Select SD card** — the wizard scans for removable drives sized 2–256 GB and suggests the most likely candidate. You can confirm, wait for a different card to be inserted, or pick from a list of all drives manually.

2. **Download Pi OS** — if no image is cached, the wizard downloads the latest Pi OS Lite 64-bit image (~550 MB compressed) from `downloads.raspberrypi.com` and caches it for future runs.

3. **Configure** — you're prompted for:
   - WiFi SSID and password (saved to `.cache/settings.json` for next run)
   - Linux username and password (defaults: `hokku` / `hokku`)
   - SSH enabled? (strongly recommended)
   - Samba (Windows file share) installed? (uses the same credentials)
   - WiFi country code (ISO 3166, e.g. `GB`, `US`, `NL`)
   - Timezone (IANA, e.g. `Europe/London`, `America/Chicago`)

4. **Write** — after you type `YES` to confirm, the image is decompressed on the fly and written to the SD card (~2.5 GB, takes 3–10 minutes). First-boot scripts are injected to configure the OS and install the hokku-server `.deb` automatically.

5. **Wait for the Pi** — insert the card, power on, and the wizard polls `hokku-server.local` until the webserver responds. First boot installs ~90 packages; on a Pi Zero 2 W allow 3–8 minutes. If SSH is enabled you can tail `/var/log/hokku-firstboot-install.log` in another window while you wait.

**Configure + flash [3] / Configure only [4] / Flash only [5] — ESP32 frame**

1. **Device detection** — the wizard scans USB serial ports for an ESP32-S3. If multiple devices are found you'll be asked to pick one. The device's current firmware version and config are displayed.

2. **Configuration prompts** (options 3 and 4) — you're asked for WiFi SSID, WiFi password, server IP, server port (default `8080`), and an optional screen name. The wizard checks the server is reachable before writing; if it isn't you'll see a warning and can continue anyway.

3. **Firmware download and flash** (options 3 and 5) — the wizard fetches the latest `hokku-firmware_*.bin` from GitHub releases (or imports a local build if one exists) and flashes it over USB. Takes about 30 seconds. A boot check follows: the wizard reads serial output for 10 seconds and reports whether the firmware started cleanly.

![Setup tool configuring a frame](../images/configurator.png)

**Advanced [6]**

- **Show / edit install settings** — view or change the cached WiFi, user, SSH, country, timezone values. Passwords can be revealed on request.
- **Download everything into .cache** — prefetch the Pi OS image, hokku-server `.deb`, and firmware so a subsequent install can run fully offline.
- **Clear .cache** — frees disk space; the next run re-downloads anything it needs.

---

## 3. Troubleshooting

**`esptool not installed`** — activate your `.venv` and run `pip install -r requirements.txt` from the project root.

**`No serial devices found`** — the cable is charge-only, or the driver isn't installed. On Windows, install the [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) if the port doesn't appear in Device Manager.

**Server IP warning during configure** — the wizard checks the server is reachable before writing config. If your server isn't running yet, proceed anyway; the frame will retry on its own schedule once the server is up.

**Pi SD card imaging requires Windows and admin** — on other platforms use the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) manually and follow [section 1.2](#12-set-up-a-raspberry-pi-optional).

**First boot very slow** — normal. The Pi installs ~90 Debian packages on first boot and waits for NTP clock sync before running `apt update`. On slow SD cards or a congested network this can push past 8 minutes. SSH in and tail `/var/log/hokku-firstboot-install.log` if you want to watch.

**Frame shows an error on the e-paper** — the firmware renders configuration and connectivity errors directly on screen. Read the message and fix the relevant setting (wrong WiFi password, wrong server IP, etc.), then re-run configure.

**Boot check reports unknown** — the frame may have entered deep sleep immediately after flashing. This is normal; it doesn't mean the flash failed. Check the web app to see if the frame checks in on its next scheduled refresh.
