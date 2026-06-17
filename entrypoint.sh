#!/bin/sh
set -e

# Ensure data directories exist with correct ownership.
# When an empty volume is mounted at /opt/htmlhost/data by Kubernetes/Docker,
# it is owned by root:root, which prevents the non-root htmlhost user from
# writing to it. This entrypoint runs as root to fix permissions before
# dropping to the application user.

DATA_DIR="${DATA_DIR:-/opt/htmlhost/data}"

mkdir -p "$DATA_DIR/uploads"
chown -R htmlhost:htmlhost "$DATA_DIR"

exec gosu htmlhost "$@"
