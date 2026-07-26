# E2E test image: install the hokku-server .deb exactly like an appliance would,
# then run the full upload+dither validation loop against the installed server.
#
# The build context is a flat staging dir assembled by run.sh (NOT the repo
# root) so the context stays tiny and the deb-install layer caches cleanly:
#   ./hokku-server.deb   ./images/   ./e2e_deb_smoke.py
#   ./expectations.json  ./test-config.json  ./entrypoint.sh
FROM debian:bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

# ca-certificates: the deb postinst pip-installs numba/opencv/heif/... over https.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the package. `apt-get install ./file.deb` resolves the declared apt
# Depends (flask, waitress, pil, numpy, ...) AND runs the postinst, which pip-
# installs the non-Debian deps (numba, opencv-python-headless, pillow-heif,
# pillow-avif-plugin, pillow-jxl-plugin, resvg-py, colour-science, ...). This is
# the slow layer; it re-runs only when the deb bytes change.
COPY hokku-server.deb /tmp/hokku-server.deb
RUN apt-get update \
    && apt-get install -y /tmp/hokku-server.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/hokku-server.deb

# Test payload (small, changes often → kept in late layers).
WORKDIR /opt/e2e
COPY images/ /opt/e2e/images/
COPY e2e_deb_smoke.py test-config.json expectations.json expectations-constrained.json entrypoint.sh /opt/e2e/
RUN chmod +x /opt/e2e/entrypoint.sh

ENTRYPOINT ["/opt/e2e/entrypoint.sh"]
