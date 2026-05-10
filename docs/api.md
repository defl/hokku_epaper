# API Reference

The image server exposes a small HTTP API used by the frames and the web app. All endpoints are under `/hokku/`.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/hokku/screen/` | GET | Next image for the frame — 960 KB binary + `X-Sleep-Seconds` header |
| `/hokku/ui` | GET | Web app |
| `/hokku/api/status` | GET | JSON status: conversion pool, screens, config, server time |
| `/hokku/api/time` | GET | Current server time in the host's timezone |
| `/hokku/api/original/<name>` | GET | Original uploaded image |
| `/hokku/api/thumbnail/<name>` | GET | 300 px thumbnail |
| `/hokku/api/dithered/<name>` | GET | Converted preview PNG |
| `/hokku/api/upload` | POST | Upload one or more images (multipart `files`) |
| `/hokku/api/image/<name>` | DELETE | Delete an image and its cached conversion |
| `/hokku/api/show_next/<name>` | POST | Queue a specific image as next |
| `/hokku/api/config` | POST | Update configuration |
| `/hokku/api/clear_cache` | POST | Wipe the conversion cache and re-convert everything |

## Frame protocol

When a frame calls `GET /hokku/screen/` the server responds with the raw image binary and an `X-Sleep-Seconds` header telling the frame how long to sleep before its next refresh. The frame never polls — it wakes on its own schedule, fetches once, then sleeps again.
