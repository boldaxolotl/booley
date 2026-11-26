# QA host: an Ubuntu container that runs its own Docker daemon (Docker-in-Docker)
# so a candidate Booley build gets its own base image, sidecars, and networks.
# See RUNBOOK.md. Build context: this directory.

# Static Docker engine, CLI plugins and the dind helper scripts.
FROM docker:29-dind AS engine

FROM ubuntu:26.04
# iptables: the inner daemon's bridge networking. git, python3: Booley host
# prerequisites; python3-venv: a development wheel builds its own wheel in a
# venv. socat: forwards the T3 Code relay port to the outer host.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git iproute2 iptables pigz procps python3 python3-venv \
      socat xz-utils \
 && rm -rf /var/lib/apt/lists/*
COPY --from=engine /usr/local/bin/ /usr/local/bin/
COPY --from=engine /usr/local/libexec/docker/ /usr/local/libexec/docker/
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

# Booley must run as uid 1000 (the Sandbox `agent` uid). Ubuntu ships an
# `ubuntu` user with uid 1000; rename it to `qa`. The `docker` group owns the
# inner daemon's socket.
RUN usermod -l qa -d /home/qa -m ubuntu && groupmod -n qa ubuntu \
 && groupadd -r docker && usermod -aG docker qa

# Stand-in VS Code CLI; Host Bootstrap needs none, a follow-up to #5603 removes it.
COPY code-stub.sh /usr/local/bin/code
COPY entrypoint.sh /usr/local/bin/qa-host-entrypoint
VOLUME /var/lib/docker
VOLUME /home/qa
ENTRYPOINT ["qa-host-entrypoint"]
