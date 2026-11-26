#!/bin/sh
# Start the inner Docker daemon, then forward the T3 Code relay ports from the
# container's network interface to its loopback, where the relay listens.
set -eu
: "${QA_HOST_RELAY_PORTS:=3780 3781 3782 3783 3784}"

# Booley reads /.dockerenv as "inside a Sandbox" and refuses host commands.
# This container plays a host, so drop the marker Docker writes into it.
rm -f /.dockerenv

# dockerd-entrypoint.sh wraps dockerd in the `dind` script (cgroup v2 nesting).
# No TLS: the socket stays inside this container.
DOCKER_TLS_CERTDIR= dockerd-entrypoint.sh dockerd --group docker \
  >/var/log/dockerd.log 2>&1 &
dockerd_pid=$!

addr=$(hostname -i | cut -d' ' -f1)
for port in $QA_HOST_RELAY_PORTS; do
  socat "TCP-LISTEN:${port},bind=${addr},fork,reuseaddr" "TCP:127.0.0.1:${port}" &
done

# The container lives as long as the inner daemon.
wait "$dockerd_pid" || true
echo "inner dockerd exited; see /var/log/dockerd.log" >&2
exit 1
