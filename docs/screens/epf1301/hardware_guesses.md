# Hokku 13.3" WiFi E-Paper Frame — Hardware Guesses

Unverified items. Confirmed findings get moved to [`hardware_facts.md`](hardware_facts.md).

## Unconfirmed GPIO Map

| GPIO | Expected Function | Notes |
|------|------------------|-------|
| 3 | EPAPER_PWR_EN | Active HIGH — needs verification |
| 4 | CHG_EN1 | Charger enable, active LOW |
| 13 | CHG_EN2 | Charger enable, active LOW |

## Power Architecture

- **EPAPER_PWR_EN (GPIO3)**: likely controls display power supply — not yet confirmed
- **USB power**: may not be sufficient for display refresh without battery — untested in isolation

## Deep Sleep

- **Target sleep current**: ~8µA with RTC GPIO isolation — design target, not empirically measured

## USB Detection — Mechanism Inference

Why GPIO14 behaves as a host-detect rather than VBUS-detect is inferred, not schematic-confirmed:

GPIO 14 is likely wired to a charger-IC pin that asserts only after USB-BC (Battery
Charging Specification 1.2) detection determines the source is a Standard Downstream
Port (SDP) or similar data-capable host. Pure-power sources (Dedicated Charging Port /
wall warts / battery banks) don't trigger BC detection because there's no data signaling
on D+/D-, so the pin stays de-asserted (HIGH). The charger IC identity is unknown.
