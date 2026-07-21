# SDK patches for the Bigme F7 firmware

The F7 firmware builds against a pinned `xr872_sdk` checkout (see
[`../../docs/screens/bigme_f7/firmware_build.md`](../../../docs/screens/bigme_f7/firmware_build.md)).
A few one-line SDK config changes are needed that don't belong in our source tree.
Apply them once to your SDK checkout before building:

```bash
cd /path/to/xr872_sdk
git apply /path/to/hokku_epaper/firmware/bigme_f7/sdk_patches/*.patch
```

(They're plain unified diffs rooted at the SDK top, so `git apply` or
`patch -p1` both work. `git apply --check *.patch` first if you want to dry-run.)

## Patches

- **0001-lwip212-enable-mdns-queries.patch** — enable `LWIP_DNS_SUPPORT_MDNS_QUERIES`
  in the lwIP 2.1.2 `lwipopts.h`. Lets the F7 resolve `<host>.local` server URLs via
  a one-shot multicast DNS query, so it can reach an mDNS-named Hokku server the same
  way the ESP32 does. The firmware selects lwIP 2.1.2 via `__CONFIG_LWIP_VER := 20102`
  in `gcc/localconfig.mk` (the SDK default is 1.4.1, which has no mDNS resolver).
