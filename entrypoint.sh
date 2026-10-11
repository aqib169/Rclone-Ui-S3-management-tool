#!/bin/sh
set -e

# If running as root (default when the image does not set USER), fix the
# ownership of the bind-mounted data dir, then permanently drop privileges
# to the non-root app user. This keeps the app process non-root while still
# tolerating a host ./data directory that is root-owned.
if [ "$(id -u)" -eq 0 ]; then
    chown -R appuser:appuser /app/data
    exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi

exec "$@"
