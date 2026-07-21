# Pi OS image: USB serial console

The appliance image (`os/pi/`) bakes in a USB gadget serial console: the
Pi's single micro-USB data port carries both power and a login shell over
one cable. No keyboard, no monitor, no HDMI needed — just a USB cable to a
PC. This is the console of last resort for a headless appliance whose only
other interface is its own web UI.

## Why this exists

On a Pi Zero-class board there's exactly one true data-capable port. If the
appliance's network configuration is broken (wrong WiFi password, DHCP
failure, whatever), there is normally no way in at all short of pulling the
SD card. The USB gadget console fixes that: plug in a cable, get a login
prompt, regardless of network state.

## The exact configuration

Baked into the image at build time by
[`os/pi/stage-hokku/01-pi-tweaks/00-run.sh`](../os/pi/stage-hokku/01-pi-tweaks/00-run.sh):

1. **`config.txt`**: `dtoverlay=dwc2,dr_mode=peripheral`
2. **`cmdline.txt`**: append `modules-load=dwc2,g_serial`
3. **A getty on the gadget tty**: `systemctl enable getty@ttyGS0`

That's the whole thing. It was arrived at the hard way, live, against a
real Pi Zero 2 W — the two traps below cost real debugging time and are
easy to reintroduce if this ever gets "cleaned up" without this context.

## Trap 1: `serial-getty@ttyGS0` looks right but isn't

Every instinct says to enable `serial-getty@ttyGS0.service` — it's the
purpose-built template for a login prompt on a serial line, and it's what
you'd use for a real UART. **Don't.** `serial-getty@.service` has
`BindsTo=dev-ttyGS0.device`, which means systemd tears the getty down the
moment it decides the device isn't "active" — and a USB gadget tty that
loads late in boot (after `g_serial` attaches) frequently never fires the
udev event that keeps that binding satisfied. The service ends up enabled,
even shows as briefly active in the journal, but nothing is actually
reading the port.

Use the **plain, generic `getty@.service` template** instead
(`systemctl enable getty@ttyGS0`, no custom unit file). It has no such
binding, retries on its own, and is what every working guide for gadget
serial actually uses.

## Trap 2: the host-side library matters as much as the Pi-side config

Once the Pi side is correct, connecting from Windows still isn't
guaranteed to work with an arbitrary serial library. `pyserial` and .NET's
`System.IO.Ports.SerialPort` both reliably **open** the resulting `USB
Serial Device` COM port, but **writes time out** — the gadget's CDC-ACM
gets picked up by the driver but the DTR line assertion timing these
libraries use isn't enough to make the Linux tty layer actually start
draining input, so nothing flows in either direction. Hours were burned
suspecting the Pi-side getty before this was isolated.

**PuTTY's `plink` works reliably.** From the command line:

```
plink -serial COM3 -sercfg 115200,8,n,1,N
```

If scripting a non-interactive session against it, note that `plink -serial`
does **not** exit on its own when its stdin pipe closes or the remote side
logs out — it just sits there holding the port. Killing it forcefully
(`Stop-Process -Force`, `taskkill /F`) usually works, but see the wedge
warning below.

## Identifying the port (Windows)

The gadget enumerates as `USB Serial Device`, vendor ID `0x0525` (Linux
Foundation's gadget-serial VID) — not a vendor-specific chip like the
CH340/CP210x used by ESP32 boards. COM port numbers float across replugs,
so always resolve it dynamically rather than hardcoding a port:

```python
from serial.tools import list_ports
port = next(p.device for p in list_ports.comports() if p.vid == 0x0525)
```

## Known failure mode: the port can wedge at the Windows driver level

Forcefully killing a process while it holds this port open mid-read/write
can leave the CDC-ACM driver in a bad state on the Windows side — the next
`open()` (from *any* tool, `plink` or `pyserial`) either hangs indefinitely
or returns "Access is denied," and no amount of process-killing or waiting
resolves it. Nothing found in-session recovers this in software; the only
reliable fix is a **physical cable replug** or a **Pi reboot** (which
re-enumerates the gadget from the device side).

Practical implication: prefer letting a session close cleanly over
force-killing it whenever the flow allows, and if a session does get
force-killed, expect the *next* connection attempt to need a replug.

## Physical setup

Connect a USB data cable from the PC to the Pi's micro-USB port — the same
port used for power. No separate power supply is required; the cable
provides both. (A charge-only cable, common and easy to grab by mistake,
will power the Pi but carry no data at all — the gadget device simply never
enumerates. If nothing shows up, try a different cable before suspecting
the config.)
