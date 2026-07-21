# Hardware

What Hokku runs on: one or more **e-paper frames**, and one **server** that feeds
them. You can mix different frame models against the same server and the same
photo library.

## Supported frames

| Frame | Size | Panel | Board | Status |
|---|---|---|---|---|
| [**Hokku / Huessen 13.3"**](screens/huessen_epf1301/hardware.md) | 13.3" | Spectra 6, 1200×1600 | ESP32-S3 | ✅ Fully supported — the original, most thoroughly tested |
| [**Bigme F7**](#bigme-f7) | 7.3" | Spectra 6, 800×480 | XR872AT | ✅ Supported — proven end-to-end on real hardware |
| [**Seeed reTerminal E1004**](#seeed-reterminal-e1004) | 13.3" | Spectra 6, 1200×1600 | ESP32-S3 | ⚠️ Experimental — builds and passes tests, **never run on real hardware** |

All three use **E Ink Spectra 6** — the same six-ink family (black, white, yellow,
red, blue, green), so the same photo library and conversion pipeline serves all of
them.

---

## Hokku / Huessen 13.3"

<img src="../images/screen.png" width="400">

The frame this project started with, and the one with the most hours on it. Sold
under both the **Hokku Designs** and **Huessen** brands — identical hardware,
different retailer. Around $280–380 depending on where you buy.

**→ [Retailers, prices, and what to check before buying](screens/huessen_epf1301/hardware.md)**

---

## Bigme F7

The cheapest way into Hokku, and the only non-ESP32 frame supported: it runs an
**XRADIOTECH XR872AT**, which took a full reverse-engineering effort to support
(see the [reverse-engineering overview](screens/bigme_f7/reverse_engineering_overview.md)).
Custom firmware, A/B OTA updates, battery reporting and mDNS all work — proven
end-to-end on real hardware.

**Any F7 variant works.** Bigme sells the F7, F7 Lite, F7 Plus, F7 Pro, F7 Max,
F7 Ultra, F7 SE and F7+ — per the manufacturer's own FCC equivalence letter
(FCC ID `2A8EM-F7`) these **share identical PCB and hardware** and differ only by
sales region. Buy whichever variant is cheapest or available where you are.

Sold by Bigme (Bigme Cloud Literacy Technology Co., Ltd.) and the usual
marketplaces.

> **Read before you buy:** turning a stock F7 into a Hokku frame requires a
> one-time USB flash that replaces the vendor firmware, and it is a more involved
> process than the ESP32 frames — see
> [Bigme F7 bootstrap](screens/bigme_f7/bootstrap.md). Going back to stock is
> documented in [restore to stock](screens/bigme_f7/restore_to_stock.md), but
> treat this as a one-way trip unless you're comfortable with the recovery
> procedure.

---

## Seeed reTerminal E1004

> ⚠️ **Experimental — not hardware-verified.** The firmware compiles, links
> against the real ESP-IDF toolchain in CI, and passes the host test suite, but
> **it has never been flashed to a physical E1004.** The panel registers and
> pinout come from Seeed's own driver and a community Arduino port; the ESP-IDF
> SPI/DMA plumbing and the battery divider ratio are unverified on silicon. If
> you own one, [we'd love to hear how it goes](https://github.com/defl/hokku_epaper/issues).

A 13.3" Spectra 6 panel on a Seeed XIAO ESP32-S3, on the reTerminal E-Series
baseboard. Same panel family and resolution as the Hokku/Huessen frame.

- **Product page**: [seeedstudio.com/reTerminal-E1004](https://www.seeedstudio.com/reTerminal-E1004-p-6692.html)
- **Details**: [hardware facts](screens/seeedstudio_e1004/hardware_facts.md) ·
  [firmware notes](../firmware/seeedstudio_e1004/README.md)

The panel bring-up work this port is based on was contributed by
[@TaichungLester](https://github.com/TaichungLester) in
[PR #16](https://github.com/defl/hokku_epaper/pull/16).

---

## The Server

<img src="../images/pi.png" width="200">

The image server runs on almost anything — a cheap ARM board, a spare laptop, a
NAS, a desktop. The only hard requirement is **512 MB of RAM**, plus a few GB of
disk for photos. If you have a spare computer sitting around, that's your server.

A **Raspberry Pi Zero 2 W** is the recommended choice: cheap, silent, uses almost
no power, and fast enough for a multi-frame setup. It's also the board the
[appliance image](appliance.md) is built for — write one file to an SD card and
the whole setup is a web form.

If you want to buy something new, the cheapest route is AliExpress — ARM boards
and accessories go for a fraction of the prices below, at the cost of weeks of
shipping and variable quality. If you're in the US and want it tomorrow from
reputable brands, this is the recommended build:

| Item | Link | Price |
|------|------|------:|
| Raspberry Pi Zero 2 W (board + essentials kit) | [Amazon](https://amzn.to/4wz5jtY) | $42 |
| Aluminium case with passive cooling and 4-port USB hub | [Amazon](https://amzn.to/4eLkzxk) | $9 |
| Official Pi power supply (5.1 V / 2.5 A) | [Amazon](https://amzn.to/4uhEbhy) | $22 |
| USB-C cable (data-capable, for flashing a frame) | [Amazon](https://amzn.to/4wAjkrh) | $10 |
| SanDisk 64 GB microSD card | [Amazon](https://amzn.to/4dpocGI) | $24 |
| USB SD card reader | [Amazon](https://amzn.to/49K7pwY) | $4 |
| **Total** | | **$111** |

A few notes:

- The kit (row 1) includes the Pi Zero 2 W board, header, heatsink, USB cable, and HDMI adapter.
- The case (row 2) does **not** include the board — it pairs with the kit above.
- The USB-C cable must be data-capable (not charge-only). The linked cable supports 10 Gbps / 3 A and works reliably for flashing.

> The Amazon links above are affiliate links. Clicking them costs you absolutely nothing extra — the price is identical either way. I get a few percent from Amazon, and if enough people do this, maybe one day there'll be enough in the jar to add the next screen to the lineup.

---

## Anything else?

**2.4 GHz WiFi is required.** None of the supported frames — and not the Pi Zero
2 W — support 5 GHz. Most routers broadcast both; either SSID is fine as long as
2.4 GHz is enabled.

**A data-capable USB cable** is needed for the one-time firmware flash. Charge-only
cables are common and won't work; if nothing appears when you plug in, try
another cable.

If you buy a frame from a listing not covered here, or get one working that isn't
on this page, please [open an issue](https://github.com/defl/hokku_epaper/issues)
— it'll get added either way.

---

> Prices last checked 2026-05-12. Amazon prices fluctuate; frame prices vary by
> retailer and sale.
