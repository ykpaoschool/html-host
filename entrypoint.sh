#!/bin/sh
set -e

# Ensure data directories exist with correct ownership.
# When an empty volume is mounted at /opt/htmlhost/data by Kubernetes/Docker,
# it is owned by root:root, which prevents the non-root htmlhost user from
# writing to it. This entrypoint runs as root to fix permissions before
# dropping to the application user.

# The path is intentionally not configurable: it must match the DATABASE_URL
# and UPLOAD_FOLDER defaults baked into the image (see Dockerfile). A deployer
# who wants the data somewhere specific chooses the host side of the mount
# (-v /host/dir:/opt/htmlhost/data), not this one.
mkdir -p /opt/htmlhost/data/uploads
chown -R htmlhost:htmlhost /opt/htmlhost/data

exec gosu htmlhost "$@"
