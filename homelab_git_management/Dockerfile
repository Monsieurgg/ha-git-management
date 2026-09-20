# =============================================================================
# HOMELAB GIT MANAGEMENT — DOCKERFILE
# =============================================================================
#
# Builds the Homelab Git Management application image.
#
# Security:
#   - no secret baked into the image;
#   - no private key copied into the image (the SSH keypair is generated
#     at runtime, under /data, which is a persistent volume outside the
#     image);
#   - GitHub is reached read-only, through the generated Deploy Key;
#   - the HTTP interface uses only the Python standard library, no third
#     party web framework;
#   - there is no manifest of managed files baked into the image anymore
#     (see homelab_git_management/config.yaml): the list of files this
#     add-on manages comes exclusively from user-supplied options at
#     runtime (/data/options.json), read by app/gestionnaire.py.
#
# =============================================================================

# hadolint ignore=DL3007
# "latest" is intentional here, not an oversight: this multi-arch image is
# maintained by the Home Assistant team specifically for local add-ons,
# and this exact reference is already running in production for this
# add-on's own private predecessor. Pinning it to a digest would need an
# update mechanism this repository does not have yet.
FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

# py3-yaml: used only for a best-effort YAML *syntax* check right before
# any write into Git or Home Assistant (gestionnaire.verifier_syntaxe_yaml),
# catching an obviously broken file before it ever reaches either side.
# This add-on's own options still come from /data/options.json (JSON,
# stdlib json module only) — this is unrelated to that.
#
# py3-openpyxl: used only to generate/read the bulk mappings Excel
# workbook (interface.py's export/import). Unrelated to the two points
# above; the engine (gestionnaire.py) has no notion of Excel at all.
RUN apk add --no-cache \
    python3 \
    py3-yaml \
    py3-openpyxl \
    git \
    openssh-client-default \
    openssh-keygen

# Startup script.
COPY run.sh /

# Comparison / policy / deployment engine.
COPY app/gestionnaire.py /app/gestionnaire.py

# Ingress web interface.
COPY app/interface.py /app/interface.py

RUN chmod a+x /run.sh

CMD ["/run.sh"]
