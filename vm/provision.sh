#!/usr/bin/env bash
# One-time provisioning: Fedora 45 Cloud Base -> var/base.qcow2 (a GNOME
# Workstation with autologin as "tester", sshd, AT-SPI, python evdev/dogtail).
#
#   vm/provision.sh [--force]
#
# Steps: verified download, 40G provisioning overlay, boot with cloud-init
# (NoCloud over a throwaway local http server, via the smbios serial), run
# vm/guest-provision.sh over ssh, power off, flatten into base.qcow2 and make
# it read-only. Takes ~10-20 minutes (mostly the Workstation dnf install).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

force=0
[[ ${1:-} == --force ]] && force=1
if [[ -e $E2E_BASE && $force -eq 0 ]]; then
    die "$E2E_BASE exists (read-only base). Use --force to rebuild it."
fi
qemu_running && die "a VM is already running (vm/stop.sh first)"

mkdir -p "$E2E_VAR/ssh" "$E2E_RUN" "$E2E_VAR/seed"
chmod 700 "$E2E_VAR/ssh"
check_accel

# --- dedicated key (never the owner's keys) --------------------------------
if [[ ! -f $E2E_SSH_KEY ]]; then
    ssh-keygen -q -t ed25519 -N '' -C "gnome-e2e tester" -f "$E2E_SSH_KEY"
fi

# --- image -----------------------------------------------------------------
image=$("$E2E_ROOT/vm/fetch-image.sh")
qemu_img --version >/dev/null

# --- provisioning overlay (bigger than the 5G image; grown by cloud-init) --
prov=$E2E_VAR/provision.qcow2
rm -f "$prov"
qemu_img create -q -f qcow2 -b "$image" -F qcow2 "$prov" "$E2E_DISK_SIZE"

# --- cloud-init NoCloud seed over http --------------------------------------
pubkey=$(<"$E2E_SSH_KEY.pub")
sed "s|@PUBKEY@|$pubkey|" "$E2E_ROOT/vm/user-data.in" >"$E2E_VAR/seed/user-data"
printf 'instance-id: gnome-e2e-provision-%s\nlocal-hostname: gnome-e2e\n' "$(date +%s)" >"$E2E_VAR/seed/meta-data"
: >"$E2E_VAR/seed/vendor-data"
seed_port=$(python3 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
python3 -I -m http.server "$seed_port" --bind 127.0.0.1 --directory "$E2E_VAR/seed" \
    >"$E2E_RUN/seed-http.log" 2>&1 &
seed_pid=$!
cleanup() { kill "$seed_pid" 2>/dev/null || true; qemu_quit || true; }
trap cleanup EXIT

# --- boot -------------------------------------------------------------------
: >"$E2E_SERIAL_LOG"; rm -f "$E2E_QMP_SOCK" "$E2E_PIDFILE"
build_qemu_args "$prov"
QEMU_ARGS+=(-smbios "type=1,serial=ds=nocloud;s=http://10.0.2.2:$seed_port/")
log "booting provisioning VM (cloud-init seed on :$seed_port)"
tb "${QEMU_ARGS[@]}"
wait_for_ssh 400 || { tail -n 40 "$E2E_SERIAL_LOG" >&2; die "guest never answered on ssh (see $E2E_SERIAL_LOG)"; }
log "ssh is up; waiting for cloud-init to finish (disk grow etc.)"
vm_ssh sudo cloud-init status --wait >&2 || log "cloud-init status reported a problem; continuing"
vm_ssh 'df -h / | tail -n1' >&2

# --- install and configure --------------------------------------------------
log "running guest provisioning (log: $E2E_VAR/provision.log)"
vm_ssh sudo bash -s <"$E2E_ROOT/vm/guest-provision.sh" 2>&1 | tee "$E2E_VAR/provision.log" | tail -n 25 >&2
[[ ${PIPESTATUS[0]} -eq 0 ]] || die "guest provisioning failed (see $E2E_VAR/provision.log)"
vm_ssh cat /var/tmp/e2e-packages.txt >"$E2E_VAR/base-packages.txt"
vm_ssh cat /var/tmp/e2e-os-release.txt >"$E2E_VAR/base-os-release.txt"

# --- power off and keep the result ------------------------------------------
log "powering off"
vm_ssh sudo systemctl poweroff || true
for _ in $(seq 1 120); do qemu_running || break; sleep 1; done
qemu_running && die "VM did not power off"
trap - EXIT
kill "$seed_pid" 2>/dev/null || true

log "flattening into base.qcow2"
[[ -e $E2E_BASE ]] && chmod u+w "$E2E_BASE" && rm -f "$E2E_BASE"
qemu_img convert -O qcow2 "$prov" "$E2E_BASE.tmp"
mv "$E2E_BASE.tmp" "$E2E_BASE"
chmod a-w "$E2E_BASE"
rm -f "$prov"
cp "$E2E_VAR/dl/IMAGE.txt" "$E2E_VAR/base-info.txt"
printf 'provisioned=%s\n' "$(date -u +%FT%TZ)" >>"$E2E_VAR/base-info.txt"
log "done: $E2E_BASE ($(du -h "$E2E_BASE" | cut -f1)), read-only"
