# Local test server

A ready-to-run dev instance of the hokku image server, for poking at the web UI,
the "Flash a screen" flow, and image rendering without a real deployment.

It runs off the committed sample images in [`images/test/`](../images/test/) and
writes its rendered cache to `test_server/cache/` (gitignored). No secrets are
committed.

## Run it

From the **repository root** (the config uses repo-relative paths):

```sh
# if the package is installed (pip install -e python/):
hokku-server test_server/config.json

# or straight from the source tree:
PYTHONPATH=python python -m hokku.webserver test_server/config.json
```

Then open <http://localhost:8080/hokku> (also advertised over mDNS as
`hokku-test.local`). A screen can point at `http://<this-host>:8080/hokku/screen/`.

## Local overrides (Wi-Fi, tuned dither, etc.)

The committed `config.json` is intentionally minimal — image-processing settings
fall back to the code defaults, and it carries no Wi-Fi credentials. Keep machine-
specific settings (flash Wi-Fi creds for the provisioning form, a tuned dither
config, a different port) in `test_server/config.local.json`, which is gitignored:

```sh
PYTHONPATH=python python -m hokku.webserver test_server/config.local.json
```

## Automated smoke test

`python/tests/test_server_smoke.py` boots this exact entrypoint as a subprocess
against `images/test/` and a temp cache, then asserts it comes up and serves a
rendered frame — a system-level check that the server actually starts, binds, and
renders. It runs in CI (marked `slow`).
