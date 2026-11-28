#!/bin/sh
# Print the primary daemon's image, container and network IDs, one sorted
# line each, so two snapshots can be compared with `diff`. Run it on the outer
# host, never inside the QA host. Container status is left out: uptime changes.
set -eu
docker images --no-trunc --format 'image {{.ID}} {{.Repository}}:{{.Tag}}' | sort
docker ps -a --no-trunc --format 'container {{.ID}} {{.Names}} {{.Image}}' | sort
docker ps -a --no-trunc -q | xargs -r docker inspect --format 'container-image {{.Name}} {{.Image}}' | sort
docker network ls --no-trunc --format 'network {{.ID}} {{.Name}}' | sort
