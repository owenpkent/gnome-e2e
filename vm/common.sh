#!/usr/bin/env bash
# Shared configuration and helpers for the vm/*.sh scripts. Source, do not run.
#
# Everything is overridable from the environment (E2E_*). Large artefacts live
# in $E2E_VAR (default: <repo>/var, gitignored).

if [[ -n "${_E2E_COMMON_LOADED:-}" ]]; then return 0; fi
_E2E_COMMON_LOADED=1

E2E_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
E2E_VAR=${E2E_VAR:-$E2E_ROOT/var}
E2E_RUN=$E2E_VAR/run

# Toolbox that provides qemu (the host has none).
E2E_TOOLBOX=${E2E_TOOLBOX:-gnome-shell-devel}
# Guest hardware.
E2E_CPUS=${E2E_CPUS:-4}
E2E_MEM=${E2E_MEM:-6G}
E2E_RES_X=${E2E_RES_X:-1280}
E2E_RES_Y=${E2E_RES_Y:-800}
E2E_DISK_SIZE=${E2E_DISK_SIZE:-40G}
# Host side ssh forward (127.0.0.1 only).
E2E_SSH_PORT=${E2E_SSH_PORT:-2222}
# kvm (default) or tcg. TCG is far too slow for a GNOME session; it only
# exists so the scripts can be exercised when /dev/kvm is not usable.
E2E_ACCEL=${E2E_ACCEL:-kvm}
E2E_USER=tester
# Seconds to wait for the guest session to come up.
E2E_BOOT_TIMEOUT=${E2E_BOOT_TIMEOUT:-300}

E2E_SSH_KEY=$E2E_VAR/ssh/id_ed25519
E2E_BASE=$E2E_VAR/base.qcow2
E2E_OVERLAY=$E2E_RUN/overlay.qcow2
E2E_QMP_SOCK=$E2E_RUN/qmp.sock
E2E_PIDFILE=$E2E_RUN/qemu.pid
E2E_SERIAL_LOG=$E2E_RUN/serial.log

log() { printf '[e2e] %s\n' "$*" >&2; }
die() { printf '[e2e] ERROR: %s\n' "$*" >&2; exit 1; }

# Run a command inside the toolbox container. XDG_DATA_DIRS is reset because
# a VS Code snap terminal leaks its own value, which shadows /usr/local
# schemas inside the container.
tb() {
    toolbox run --container "$E2E_TOOLBOX" \
        env XDG_DATA_DIRS=/usr/local/share:/usr/share "$@"
}

# ---------------------------------------------------------------------------
# qemu-img: the toolbox ships qemu-system but not qemu-img, and toolbox sudo
# needs a password. vm/get-qemu-img.sh downloads the signed Fedora RPM and
# unpacks it under var/tools without installing anything.
qemu_img() {
    local local_bin=$E2E_VAR/tools/root/usr/bin/qemu-img
    if [[ ! -x $local_bin ]] && ! tb sh -c 'command -v qemu-img' >/dev/null 2>&1; then
        "$E2E_ROOT/vm/get-qemu-img.sh" || die "cannot obtain qemu-img"
    fi
    if [[ -x $local_bin ]]; then
        tb "$local_bin" "$@"
    else
        tb qemu-img "$@"
    fi
}

# ---------------------------------------------------------------------------
# ssh / scp to the guest as the dedicated tester key (never the owner's keys).
E2E_SSH_OPTS=(
    -F /dev/null
    -i "$E2E_SSH_KEY" -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=5
    -o ServerAliveInterval=10 -o ServerAliveCountMax=6
)

vm_ssh() {
    ssh "${E2E_SSH_OPTS[@]}" -p "$E2E_SSH_PORT" "$E2E_USER@127.0.0.1" "$@"
}

vm_scp() {
    scp "${E2E_SSH_OPTS[@]}" -P "$E2E_SSH_PORT" "$@"
}

# Wait until sshd answers, up to $1 seconds.
wait_for_ssh() {
    local timeout=${1:-120} deadline=$((SECONDS + ${1:-120}))
    while (( SECONDS < deadline )); do
        if vm_ssh true 2>/dev/null; then return 0; fi
        sleep 2
    done
    return 1
}

# Guest-side probe: succeeds (exit 0) once tester's gnome-shell is up and
# answering on its D-Bus name. The session env comes from /run/user/<uid>.
# Written without grep/rg so it works on a minimal guest.
GUEST_SHELL_UP_SCRIPT='
uid=$(id -u tester) || exit 1
rt=/run/user/$uid
[ -S "$rt/bus" ] || exit 2
export XDG_RUNTIME_DIR=$rt DBUS_SESSION_BUS_ADDRESS=unix:path=$rt/bus
pgrep -u "$uid" -x gnome-shell >/dev/null || exit 3
ver=$(busctl --user get-property org.gnome.Shell /org/gnome/Shell org.gnome.Shell ShellVersion 2>/dev/null) || exit 4
case "$ver" in s\ *) ;; *) exit 5 ;; esac
for s in "$rt"/wayland-*; do
    case "$s" in *.lock) continue ;; esac
    [ -S "$s" ] && exit 0
done
exit 6
'

# Wait for the GNOME Shell session of tester, up to $1 seconds.
wait_for_shell() {
    local timeout=${1:-$E2E_BOOT_TIMEOUT} deadline=$((SECONDS + ${1:-$E2E_BOOT_TIMEOUT}))
    local rc=0
    while (( SECONDS < deadline )); do
        if vm_ssh bash -s <<<"$GUEST_SHELL_UP_SCRIPT" 2>/dev/null; then return 0; fi
        sleep 2
    done
    return 1
}

# ---------------------------------------------------------------------------
# QMP helper (stdlib python on the host).
qmp() { python3 -I "$E2E_ROOT/e2e/qmpctl.py" "$E2E_QMP_SOCK" "$@"; }

qemu_running() {
    [[ -s $E2E_PIDFILE ]] && kill -0 "$(<"$E2E_PIDFILE")" 2>/dev/null
}

# Check that the toolbox can use /dev/kvm. Access is a seat ACL, so it
# disappears when nobody is logged in at the console (see README).
check_accel() {
    if [[ $E2E_ACCEL == kvm ]]; then
        if ! tb sh -c 'test -r /dev/kvm && test -w /dev/kvm'; then
            die "the $E2E_TOOLBOX toolbox cannot open /dev/kvm (the ACL follows the console seat login). Log in at the console, or E2E_ACCEL=tcg for a (very slow) smoke boot."
        fi
    fi
}

# Build the qemu command line into the global array QEMU_ARGS.
#   $1 = drive file, $2 = extra args are appended by the caller.
build_qemu_args() {
    local drive=$1
    local accel_args cpu
    if [[ $E2E_ACCEL == kvm ]]; then
        accel_args=(-machine q35,accel=kvm); cpu=host
    else
        accel_args=(-machine q35,accel=tcg); cpu=max
    fi
    mkdir -p "$E2E_RUN"
    QEMU_ARGS=(
        qemu-system-x86_64
        -name gnome-e2e
        "${accel_args[@]}"
        -cpu "$cpu"
        -smp "$E2E_CPUS" -m "$E2E_MEM"
        -rtc base=utc
        -display none
        -vga none
        -device "virtio-vga,xres=$E2E_RES_X,yres=$E2E_RES_Y"
        -device virtio-keyboard-pci
        -device virtio-rng-pci
        -drive "file=$drive,if=virtio,format=qcow2,discard=unmap,cache=writeback"
        -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$E2E_SSH_PORT-:22"
        -device virtio-net-pci,netdev=n0
        -qmp "unix:$E2E_QMP_SOCK,server=on,wait=off"
        -serial "file:$E2E_SERIAL_LOG"
        -pidfile "$E2E_PIDFILE"
        -daemonize
    )
}

# Ask a running qemu to quit and wait for it to disappear.
qemu_quit() {
    local pid
    qemu_running || return 0
    pid=$(<"$E2E_PIDFILE")
    qmp quit >/dev/null 2>&1 || kill "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
}
