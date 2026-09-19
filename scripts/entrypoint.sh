#!/bin/sh
# Container entrypoint. Prepares the persistent-state volume, then drops
# privileges and execs whatever the CMD (or `docker run` override) asks for.
#
# The chown handles the common case where /data is mounted from the host
# and lands owned by root — runuser can't write there as `observer` until
# we hand ownership over. If /data is a docker-managed volume it's already
# owned by observer, but chown -R is idempotent + cheap.
set -e

mkdir -p /data
chown -R observer:observer /data

# Drop privileges and exec so signals reach uvicorn cleanly.
exec runuser -u observer -- "$@"
