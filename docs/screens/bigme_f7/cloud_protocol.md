# Bigme F7 — Cloud Protocol

Reverse-engineered from string literals in the boot partition binary (2026-06-06).
All strings are null-terminated C literals extracted directly from the firmware; they are facts, not guesses.
Unknown fields and inferred behaviour are marked.

## Architecture

```
Device → WiFi → Internet → ereader.bigme.vip:8086  (HTTP)
Device → WiFi → Internet → 120.76.40.178:1883       (MQTT)
```

The device uses two channels:
1. **Periodic HTTP poll** — device calls `/PhotoFrameDeviceStatus` on a schedule to get the current image and settings.
2. **MQTT push** — broker pushes an `updatePicture` action message to trigger an immediate image refresh between polls.

## Identity

| Field | Value / Format |
|---|---|
| Device ID | `BIGME_<MAC>` — e.g. `BIGME_189E2DF98754` |
| Setup AP SSID | `BigmeFrameRouter` |
| Setup AP password | `88888888` |
| Operational AP (SSID) | `XRZ_<MAC>` (used during WiFi provisioning phase) |
| Firmware version key | `fwVer_%s` → value `1.2.7` |
| Serial number key | `SN:%s;VR:%s;` (sent in some payloads) |

## MQTT

- **Broker**: `120.76.40.178:1883`
- **Username**: `mqt_user`
- **Password**: `xrz86112763`
- **Subscribe topic**: `iot/device/<deviceId>`
- **Will topic**: `iot/device/willTopic`
- **Server-publish topic**: `iot/device/sever_topic` (sic — typo in firmware; "sever" not "server")

### MQTT message format

Incoming message fields (from firmware string `action:%s` and `action` key):

```json
{
  "action": "updatePicture"
}
```

When `action == "updatePicture"` the device fetches a new image using the HTTP `/PhotoFrameDeviceStatus` poll (see below), or re-uses the URL from a prior response.

## HTTP API

Base URL: `http://ereader.bigme.vip:8086`

All requests appear to be HTTP GET or POST with URL-encoded parameters or JSON bodies. The exact method (GET/POST) and body format require network traffic capture to confirm.

### POST `/TableCard/TerminalKey`

Device registration / key exchange. Called on first boot or rebind.

Request fields (inferred from string `deviceSn`, `userId`):

| Field | Description |
|---|---|
| `deviceSn` | Device serial number |
| `userId` | User account ID |

### POST `/PhotoFrameBind`

Bind device to user account. Response contains `code` (success/fail) and `bindState`.

Debug strings: `bind userId:%s`, `bind deviceName:%s`, `bind url:%s`, `bind outBuf= %s`, `bind true`, `bind false`.

### POST `/PhotoFrameUnBind`

Unbind device from user account.

### POST `/PhotoFrameDeviceStatus`

**Main heartbeat + image update endpoint.** Called periodically (interval from `refreshInterval` in prior response) and after MQTT push.

**Request** (URL-encoded query string from firmware literal):

```
ssid=%s&psk=%s&sn=%s&picVersion=%s&log=%c&userId=%s&deviceName=%s&reboot=%c&&apmode=%c&wakeupTime1=%s&wakeupTime2=%s&
```

| Parameter | Description |
|---|---|
| `ssid` | Current WiFi SSID |
| `psk` | WiFi password |
| `sn` | Device serial number |
| `picVersion` | MD5 or version tag of the currently displayed image |
| `log` | Unknown (single char, likely a boolean flag) |
| `userId` | User account ID |
| `deviceName` | Friendly device name |
| `reboot` | Unknown (single char flag) |
| `apmode` | Whether device is in AP mode (char flag) |
| `wakeupTime1` | Scheduled wake-up time 1 (format `HH:MM`, default `08:00`) |
| `wakeupTime2` | Scheduled wake-up time 2 (format `HH:MM`, default `20:00`) |

Additional fields sent in some calls (from separate string literals): `deviceName`, `battery`, `charge`, `fwVersion`.

**Response** (JSON, fields extracted from parser strings):

```json
{
  "refreshInterval": 3600,
  "systemTime": "...",
  "amClock": "08:00",
  "pmClock": "20:00",
  "amDistance": 0,
  "pmDistance": 0,
  "bindState": 1,
  "getPhoto": {
    "pictureUrl": "http://...",
    "pictureMd5": "abc123..."
  },
  "otaUpdate": {
    "otaUrl": "http://..."
  }
}
```

| Field | Type | Description |
|---|---|---|
| `refreshInterval` | int | Seconds between polls |
| `systemTime` | string | Server time (format unknown) |
| `amClock` | string | Morning wake-up time |
| `pmClock` | string | Evening wake-up time |
| `amDistance` | long | Unknown — wake-up interval variant |
| `pmDistance` | long | Unknown — wake-up interval variant |
| `bindState` | int | 1 = bound, 0 = not bound |
| `getPhoto.pictureUrl` | string | URL to download the new image |
| `getPhoto.pictureMd5` | string | MD5 hash of the image for integrity check |
| `otaUpdate.otaUrl` | string | URL of OTA firmware binary (absent if no update) |

The device compares `pictureMd5` against the stored `picVersion`. If they differ, it downloads `pictureUrl`, verifies the MD5, writes to internal flash, then displays it.

### POST `/PhotoFrameFwCheckFinal`

OTA firmware check. Called when `otaUpdate.otaUrl` is present. Fields include `fwVersion`.

## Image Format

**Unknown — needs traffic capture.** See [`hardware_guesses.md`](hardware_guesses.md) for the reasoning.

Best guess: JPEG at 800×480, served via plain HTTP from `pictureUrl`. The XR872AT has a hardware JPEG decoder (no software library found in 4 MB flash dump). The device downloads the file, verifies the MD5, writes to spare flash, then hardware-decodes and sends pixels to the EPD via SPI.

## Integration with hokku_epaper

To make hokku_epaper serve a Bigme F7 without cloud dependency, two approaches:

### Option A — DNS redirect (no flash modification, recommended for testing)

1. On the LAN/router, resolve `ereader.bigme.vip` → hokku_epaper server IP.
2. Implement the `/PhotoFrameDeviceStatus` endpoint on port 8086.
3. Return a JSON response with `getPhoto.pictureUrl` pointing to a generated image on hokku_epaper.
4. Serve the image at that URL in the format the device expects.

The device will continue to use its stock firmware; only the server hostname is redirected.

### Option B — Flash binary patch

Replace the `ereader.bigme.vip` hostname string in the boot partition binary (at raw flash offset ~0x010B57 in a 4 MB dump), reflash via PhoenixMC. **Caveat**: the AWIH header contains a CRC/hash at +0x08; modifying the payload without recomputing the hash may cause the bootloader to reject the partition. The hash algorithm is unknown.

### Option C — Custom firmware

Write a new boot partition that speaks the hokku_epaper wire protocol directly. Requires XR Skylark SDK and ARM cross-compilation toolchain (not currently available in this environment).
