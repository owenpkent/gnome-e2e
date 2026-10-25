#!/usr/bin/env bash
# The gnome-shell-devel toolbox has qemu-system-x86_64 but no qemu-img, and
# installing packages there needs a sudo password. This downloads Fedora's
# qemu-img RPM with dnf5 (as the user, nothing installed), checks its package
# signature against the key already in the toolbox rpmdb, and unpacks it into
# var/tools/root. Only runs when qemu-img is missing.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

root=$E2E_VAR/tools/root
if [[ -x $root/usr/bin/qemu-img ]]; then exit 0; fi

log "fetching qemu-img RPM through the $E2E_TOOLBOX toolbox (no install)"
rm -rf "$E2E_VAR/tools/rpm" "$root"
mkdir -p "$E2E_VAR/tools/rpm" "$root"
tb sh -c "cd '$E2E_VAR/tools/rpm' && dnf5 download qemu-img"
rpm=$(ls "$E2E_VAR"/tools/rpm/qemu-img-*.rpm | head -n1)
tb rpm -K "$rpm" | tee /dev/stderr | { read -r line; [[ $line == *"signatures OK"* ]]; } \
    || die "RPM signature check failed for $rpm"
tb sh -c "cd '$root' && rpm2cpio '$rpm' | cpio -idm --quiet"
tb "$root/usr/bin/qemu-img" --version | head -n1 >&2
