#!/bin/sh
set -eu

runtime_uid="${DAILYCAST_UID:-1000}"
runtime_gid="${DAILYCAST_GID:-1000}"

mkdir -p /app/data /app/public
chown -R "${runtime_uid}:${runtime_gid}" /app/data /app/public

exec gosu "${runtime_uid}:${runtime_gid}" "$@"
