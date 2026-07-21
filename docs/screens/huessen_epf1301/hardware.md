# Hokku / Huessen 13.3" — hardware

> Part of the [hardware overview](../../hardware.md), which covers every
> supported frame and the server kit.

## The Frame

<img src="../../../images/screen.png" width="400">

This project is built for the **Hokku Designs / Huessen 13.3" WiFi E-Paper Art Photo Frame** — a six-colour Spectra 6 e-ink display with an ESP32-S3 inside. It is sold under two brand names depending on the retailer: **Hokku Designs** and **Huessen**. The hardware is identical.

The following retailers stock it. The Wayfair listing is the one confirmed to contain the exact hardware this firmware targets. The others are likely the same but have not been independently verified.

| Retailer | Listing | Price | Confirmed |
|----------|---------|------:|:---------:|
| Wayfair | [Hokku Designs 13.3" WiFi E-Paper Art Photo Frame](https://www.wayfair.com/decor-pillows/pdp/hokku-designs-133-inch-wifi-epaper-art-photo-frame-w115006181.html) | $280 | ✓ |
| Macy's | [Huessen 13.3" WiFi E-Paper Art Digital Photo Frame](https://www.macys.com/shop/product/huessen-13.3-inch-wifi-epaper-art-digital-photo-frame?ID=23769763) | $320 | |
| Best Buy | [Huessen 13.3" WiFi E-Paper Art Photo Frame](https://www.bestbuy.com/product/huessen-13-3-inch-wifi-epaper-art-photo-frame/J3KVWY3Q9L) | $350 | |
| eco4life | [13.3" WiFi E-Ink Art Photo Frame](https://mall.eco4lifehome.com/products/13-3-inch-wifi-e-ink-art-photo-frame-smart-epaper-digital-display-with-app-control-cloud-sync-no-glare-no-light-pollution-ultra-low-power-ideal-for-home-office-decor) | $380 | |

If you buy one of the unconfirmed listings above — or find the frame somewhere else entirely — please [open a GitHub issue](https://github.com/defl/hokku_epaper/issues) with what you bought and whether it worked. I'll add it to the list either way.

### What to check before buying

- **Size**: 13.3". Smaller or larger frames from the same brand family are different hardware and are not compatible with this firmware.
- **Six colours**: the listing should mention red, yellow, blue, and green in addition to black and white. Frames with only black and white, or black/white/red, use a different display technology and are not compatible.
- **WiFi**: confirmed 2.4 GHz. The frame does not support 5 GHz networks.

---

## The Server

The server hardware is the same whichever frame you use — see
[**The Server**](../../hardware.md#the-server) in the hardware overview for the
recommended Raspberry Pi kit, or [the appliance image](../../appliance.md) for the
no-terminal setup route.

---

> Prices last checked 2026-05-12. The frame price varies by retailer and sale.
