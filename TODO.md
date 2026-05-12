# TODO

Items captured during development that aren't urgent enough to block current
work but worth picking up later. Add new entries at the top.

- Big pipeline update
```
Several meaningful gaps for e-ink specifically:

1. Unsharp mask with configurable radius/amount — highest impact

ImageEnhance.Sharpness uses a fixed 3×3 kernel internally. You can't control the radius, so you can't distinguish "edge crispening" (small radius ~0.5px) from "local contrast pop" (large radius ~2–3px). For e-ink where the dither pattern itself softens everything, a proper USM (usm_radius, usm_amount) would give much finer control than the current prepare_sharpness scalar.

2. Local contrast (CLAHE) — e-ink specific

Global autocontrast stretches the overall histogram but can't simultaneously lift shadows and preserve highlights. CLAHE does this tile-by-tile. On a 6-color palette with very limited dynamic range, it's the most effective way to reveal mid-shadow detail that would otherwise collapse into black during dithering.

3. Pre-dither noise injection — e-ink specific

A small amount of Lab-space noise (e.g. dither_noise_l: 2.0) added before error diffusion breaks up the regular "worm" patterns that error diffusion produces in smooth gradients. On a static display you stare at for hours, those patterns are very visible. Professional print pipelines always include this.

4. Midtone control separate from gamma

prepare_gamma does a global power curve. A dedicated prepare_midtone lift (e.g. a simple 3-point: shadows / midtones / highlights) would let you brighten mids without touching the black point — useful because e-ink looks darker in typical room lighting compared to calibration conditions.

5. DRC highlight rolloff

compress_dynamic_range currently does a hard linear L* squeeze: L_out = L_in * ratio + black_L. Highlight regions (L > 80) hit the panel's white ceiling abruptly. A soft shoulder (log or S-curve rolloff above a threshold) would make bright regions transition more naturally rather than clipping flat.
```


- Firmware configurable IP, if set go to URL, if not set connect to hokku-server.local (mdns)
- mdns setup for server and client in the configurator
- Add to features that it's zeroconf by default but overridable if you want.