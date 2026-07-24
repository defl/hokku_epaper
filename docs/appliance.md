# The Hokku appliance image

The easiest way to run Hokku: a ready-made Raspberry Pi OS image with the server
already installed. Write it to a microSD card, put it in a Raspberry Pi Zero 2 W,
power it on, and configure everything from your phone over WiFi. No keyboard, no
monitor, no terminal, no `pip`.

The image boots into a setup mode that raises its own WiFi access point. You join
it, a form opens, you fill in your WiFi details, and the Pi reboots onto your real
network with the server running.

## What you need

- **A Raspberry Pi Zero 2 W** (the recommended board — see the
  [hardware guide](hardware.md#the-server) for a tested kit).
  Any Pi that runs 64-bit Raspberry Pi OS will work; the Zero 2 W is the one
  the image is built and tested against.
- **A microSD card**, 8 GB or larger. The image expands to about 3.9 GB.
- **A USB power supply** and a cable.
- **A phone or laptop with WiFi**, to run the setup wizard.
- **A 2.4 GHz WiFi network.** The Pi Zero 2 W has no 5 GHz radio.

## 1. Write the image

Download `image-<date>-hokku.img.xz` from the
[latest release](https://github.com/defl/hokku_epaper/releases).

Write it to the card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
(*Choose OS → Use custom*), [balenaEtcher](https://etcher.balena.io/), or any
image writer. Both handle the `.xz` compression for you — no need to unpack it
first.

> **Don't apply Raspberry Pi Imager's own customisation settings** (the gear icon
> — hostname, WiFi, user account). The Hokku image does all of that through its
> own setup wizard, and Imager's settings can interfere with it.

Put the card in the Pi and connect power.

## 2. Join the setup network

First boot takes a minute or two. When it's ready the Pi raises an **open WiFi
network called `Hokku Setup`** — no password.

Join it from your phone or laptop.

Most phones detect the captive portal and pop the setup page open by themselves.
If yours doesn't, browse to **http://192.168.11.1** — every DNS lookup on this
network resolves there, so almost any address you type will land on the wizard.

## 3. Fill in the wizard

| Field | What it does |
|---|---|
| **WiFi network + password** | The network the Pi will join once setup finishes. Pick from the scan list or type it in. Must be 2.4 GHz. |
| **Country** | Sets the WiFi regulatory domain. Required — the radio stays disabled without it. |
| **Hostname** | The Pi's system name on your network. |
| **Timezone** | Used for the refresh schedule, so photos change at the times you expect. |
| **Network** | DHCP (default, recommended) or a static IP with gateway and DNS. |
| **Find on network by name** | Advertises the server over mDNS so you can reach it at `http://<name>.local:8080` instead of chasing an IP. On by default. |
| **SSH** | Optional. Enables remote terminal access. |
| **File sharing (Samba)** | Optional. Exposes the photo folder as a network share. |
| **Admin password** | Optional but **strongly recommended**, especially if you enable SSH — see [Default credentials](#default-credentials) below. |

Submit the form. The Pi applies everything and reboots; it takes roughly a minute
to come back on your real network.

## 4. Open the server

Disconnect from `Hokku Setup`, rejoin your normal WiFi, and open:

```
http://<the-name-you-chose>.local:8080
```

That's the web app — upload photos, add frames, set the schedule. See the
[user manual](manual.md) for a full walkthrough.

If the `.local` name doesn't resolve (some Windows and Android setups don't do
mDNS reliably), find the Pi's IP address in your router's DHCP client list and
use `http://<ip>:8080` instead.

## Flashing a frame from the appliance

The appliance can flash a frame over USB itself — open **Flash a screen** in
the web app, no separate setup machine needed. But the Pi Zero 2 W has only a
**single USB data port**, and the appliance runs it in dual-role (OTG) mode: a
normal cable to a PC is the serial console (peripheral), while a frame on a
micro-USB→USB-A OTG adapter turns the port into a host that can flash it.

Because it's one shared port, the order matters:

1. **Boot the appliance with nothing plugged into the data port.** A frame
   attached *at boot* puts the port into host mode while the system is still
   starting, and the appliance never finishes coming up (no web app, no SSH).
   This is the most common mistake — always boot bare.
2. Power the Pi from its **PWR** port, so the data port is free to host.
3. Once the appliance is up, **hot-plug the frame** into the data port through
   the OTG adapter. The port flips to host mode, the frame enumerates, and it
   appears in **Flash a screen**.
4. Flash it, then unplug the frame. (While a frame is attached the USB serial
   console is unavailable — the port can't be host and console at once.)

If a frame you plug in doesn't show up, check the OTG adapter — host mode needs
the adapter's ID pin grounded; a plain charging cable won't switch the port.

## If something goes wrong

### The Pi never appears on your network

It fixes itself. A watchdog checks the WiFi connection after every setup: if the
Pi can't connect within about three and a half minutes of booting — wrong
password, network out of range, typo in the SSID — it **automatically reverts to
setup mode and raises the `Hokku Setup` network again**. Rejoin it and correct
the details.

If it pings but the web app and SSH never come up, check you don't have a
**frame plugged into the USB data port** — a frame attached at boot wedges
startup. Unplug it and power-cycle; see [Flashing a frame from the
appliance](#flashing-a-frame-from-the-appliance).

### `Hokku Setup` never appears

Give it a full two minutes from power-on. If it still doesn't show up, the card
may not have been written correctly — rewrite it and try again. To see what the
Pi is actually doing without a monitor, use the
[USB serial console](os_pi_usb_console.md): one cable to your computer gives you
a login prompt.

### Starting over

To send a configured appliance back to setup mode — to move it to a different
WiFi network, for example — run this on the Pi as root (over SSH or the serial
console):

```sh
sudo /usr/lib/hokku-installer/reset.sh
```

It clears the setup marker and reboots, and the `Hokku Setup` network comes back.
Your photos and settings are left alone.

## Default credentials

The image ships with a default system login:

```
username: hokku
password: hokku
```

This exists so the serial console and first-time SSH work at all. **Change it**
— either by setting an admin password in the wizard, or afterwards with `passwd`
— particularly if you enabled SSH. The Pi is only as protected as your local
network until you do.

The Hokku web app itself has no login. Anyone on your network who can reach port
8080 can manage your photo library. That's deliberate for a home appliance, but
worth knowing before you expose it any further.

## Building the image yourself

The image is built with [pi-gen](https://github.com/RPi-Distro/pi-gen) from the
recipe in [`os/pi/`](../os/pi/) — see [`os/pi/README.md`](../os/pi/README.md).
It is also built automatically by CI and attached to every GitHub release.
