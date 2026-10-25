#!/usr/bin/env bash
# Boot a fresh VM: a new qcow2 overlay on top of the read-only var/base.qcow2
# (clean state every run), then wait until tester's GNOME Shell is up.
#
#   vm/start.sh
#
# Env: E2E_SSH_PORT (2222), E2E_CPUS (4), E2E_MEM (6G), E2E_RES_X/Y (1280x800),
# E2E_BOOT_TIMEOUT (300 s), E2E_ACCEL (kvm|tcg).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

[[ -f $E2E_BASE ]] || die "$E2E_BASE missing: run vm/provision.sh first"
[[ -f $E2E_SSH_KEY ]] || die "$E2E_SSH_KEY missing: run vm/provision.sh first"
if qemu_running; then die "a VM is already running (pid $(<"$E2E_PIDFILE")); run vm/stop.sh first"; fi
check_accel

mkdir -p "$E2E_RUN"
rm -f "$E2E_OVERLAY" "$E2E_QMP_SOCK" "$E2E_PIDFILE"
: >"$E2E_SERIAL_LOG"
qemu_img create -q -f qcow2 -b "$E2E_BASE" -F qcow2 "$E2E_OVERLAY"

build_qemu_args "$E2E_OVERLAY"
log "booting (${E2E_CPUS} vCPUs, ${E2E_MEM}, ${E2E_RES_X}x${E2E_RES_Y}, ssh 127.0.0.1:${E2E_SSH_PORT})"
t0=$SECONDS
tb "${QEMU_ARGS[@]}"
qemu_running || die "qemu did not start"

if ! wait_for_ssh 150; then
    tail -n 30 "$E2E_SERIAL_LOG" >&2
    qemu_quit; die "guest sshd never answered (serial log: $E2E_SERIAL_LOG)"
fi
log "sshd up after $((SECONDS - t0)) s; waiting for GNOME Shell"
if ! wait_for_shell "$E2E_BOOT_TIMEOUT"; then
    tail -n 30 "$E2E_SERIAL_LOG" >&2
    vm_ssh 'sudo systemctl status gdm --no-pager -l | tail -n 25; sudo journalctl -b -u gdm --no-pager | tail -n 25' >&2 || true
    qemu_quit; die "GNOME Shell did not come up within ${E2E_BOOT_TIMEOUT}s"
fi
log "GNOME Shell is up after $((SECONDS - t0)) s"

# Guest helper package, copied in fresh each run.
vm_ssh 'rm -rf ~/e2e && mkdir -p ~/e2e'
vm_scp -q -r "$E2E_ROOT/guest/e2e_guest" "$E2E_USER@127.0.0.1:e2e/"
log "ready: ssh -i $E2E_SSH_KEY -p $E2E_SSH_PORT $E2E_USER@127.0.0.1"
