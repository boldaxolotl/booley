# Proof-only compatibility layer for a read-only mounted Vivado 2025.2 install.
FROM booley-sandbox

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends locales libpixman-1-0 \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*
USER agent
