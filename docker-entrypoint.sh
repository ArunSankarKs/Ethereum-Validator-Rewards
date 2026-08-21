#!/bin/sh
set -e
# Bind-mounted ./data often isn't writable by a non-root image user.
# Ensure the cache dir exists and is usable inside the container.
mkdir -p /app/data
chmod -R u+rwX /app/data 2>/dev/null || true
exec "$@"
