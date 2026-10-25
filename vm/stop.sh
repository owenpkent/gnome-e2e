#!/usr/bin/env bash
# Stop the VM (QMP quit; the overlay is disposable). The overlay and serial log
# stay in var/run until the next start.sh, for post-mortems.
#   vm/stop.sh [--clean]    --clean also deletes the overlay
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if qemu_running; then
    log "stopping VM (pid $(<"$E2E_PIDFILE"))"
    qemu_quit
else
    log "no VM running"
fi
rm -f "$E2E_QMP_SOCK" "$E2E_PIDFILE"
if [[ ${1:-} == --clean ]]; then
    rm -f "$E2E_OVERLAY"
fi
