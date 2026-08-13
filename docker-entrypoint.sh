#!/bin/sh
# Create the data subdirectories at START, not at build.
#
# /data is a named volume. Docker seeds a volume from the image ONLY when the
# volume is empty at first creation — so a `mkdir` in the Dockerfile is invisible
# to any deployment whose volume already exists. Adding STUDIO_AUDIT_LOG_PATH
# after the first `compose up` is exactly that case: the directory was in the
# image and still absent in the container, and the audit writer failed silently
# to stdout.
#
# Idempotent, cheap, and correct for both fresh and upgraded volumes.
set -e

for dir in "$LP_STATE_DIR" "$STORAGE_LOCAL_PATH" "$(dirname "$STUDIO_AUDIT_LOG_PATH")"; do
    [ -n "$dir" ] && mkdir -p "$dir"
done

exec "$@"
