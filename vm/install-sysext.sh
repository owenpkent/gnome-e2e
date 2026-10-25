#!/usr/bin/env bash
# Install the toolbox's systemd-sysext into the running guest, then restart
# the session so gnome-shell/mutter/gnome-control-center run the dev build.
#
#   vm/install-sysext.sh [--toolbox NAME] [--from-dir DIR] [--name EXTNAME]
#                        [--no-restart] [--remove]
#
# Default source: podman cp NAME:/var/lib/extensions/NAME/. (as
# gnome-shell/tools/toolbox/toolbox-sysext-install.sh does; assemble it with
# `meson-build.sh --sysext` in the toolbox). The extension-release file is the
# toolbox's /etc/os-release, so ID and VERSION_ID must match the guest.
#
# Guest side: /var/lib/extensions/NAME, SELinux relabel (setfiles, like the
# toolbox script), `systemctl restart systemd-sysext`, recompile the GSettings
# schemas into the extension, restart systemd-sysext, then restart gdm and
# wait for the autologin session to come back.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TOOLBOX=$E2E_TOOLBOX
FROM_DIR=
NAME=
RESTART=1
REMOVE=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --toolbox) TOOLBOX=$2; shift 2 ;;
        --from-dir) FROM_DIR=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --no-restart) RESTART=0; shift ;;
        --remove) REMOVE=1; shift ;;
        -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument $1" ;;
    esac
done
NAME=${NAME:-$TOOLBOX}
DEST=/var/lib/extensions/$NAME

wait_new_session() {
    local old=$1 deadline=$((SECONDS + E2E_BOOT_TIMEOUT)) pid
    while (( SECONDS < deadline )); do
        pid=$(vm_ssh 'pgrep -u tester -x gnome-shell | head -n1' 2>/dev/null || true)
        if [[ -n $pid && $pid != "$old" ]] && vm_ssh bash -s <<<"$GUEST_SHELL_UP_SCRIPT" 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

restart_session() {
    local old
    old=$(vm_ssh 'pgrep -u tester -x gnome-shell | head -n1' 2>/dev/null || true)
    log "restarting gdm and waiting for the autologin session"
    vm_ssh sudo systemctl restart gdm
    wait_new_session "$old" || die "GNOME Shell did not come back after the gdm restart"
}

qemu_running || die "no VM running (vm/start.sh first)"
vm_ssh true || die "cannot ssh into the guest"

if (( REMOVE )); then
    vm_ssh "sudo rm -rf '$DEST' && sudo systemctl restart systemd-sysext.service"
    (( RESTART )) && restart_session
    log "removed $DEST"
    exit 0
fi

work=$(mktemp -d "$E2E_VAR/sysext-XXXXXX")
trap 'rm -rf "$work"' EXIT
tree=$work/tree
mkdir -p "$tree"

if [[ -n $FROM_DIR ]]; then
    [[ -d $FROM_DIR ]] || die "--from-dir $FROM_DIR is not a directory"
    cp -a "$FROM_DIR/." "$tree/"
else
    podman container exists "$TOOLBOX" || die "toolbox container $TOOLBOX does not exist"
    if ! podman exec "$TOOLBOX" test -d "/var/lib/extensions/$TOOLBOX/usr"; then
        die "/var/lib/extensions/$TOOLBOX is missing or empty in the toolbox. Build it with: cd <project>; meson-build.sh --sysext (mutter first, then gnome-shell, ...)"
    fi
    log "podman cp $TOOLBOX:/var/lib/extensions/$TOOLBOX"
    podman cp "$TOOLBOX:/var/lib/extensions/$TOOLBOX/." "$tree"
fi

rel=$tree/usr/lib/extension-release.d/extension-release.$NAME
if [[ ! -s $rel ]]; then
    mkdir -p "$(dirname "$rel")"
    if [[ -z $FROM_DIR ]]; then
        podman cp "$TOOLBOX:/etc/os-release" "$rel"
    else
        vm_ssh cat /etc/os-release >"$rel"
    fi
fi

# Sysexts only attach when ID/VERSION_ID (or SYSEXT_LEVEL) match the host.
want=$( . "$rel"; echo "${ID:-}:${VERSION_ID:-}" )
have=$(vm_ssh '. /etc/os-release; echo "${ID:-}:${VERSION_ID:-}"')
[[ $want == "$have" ]] || die "extension-release says $want but the guest is $have"
log "extension-release matches guest ($have)"

log "copying $(du -sh "$tree" | cut -f1) into guest $DEST"
tar --owner=0 --group=0 --numeric-owner -C "$tree" -cf - . \
    | vm_ssh "sudo rm -rf '$DEST' && sudo mkdir -p '$DEST' && sudo tar -C '$DEST' -xf -"

GUEST_STEPS=$(cat <<'STEPS'
set -eu
dest=$1
if command -v setfiles >/dev/null && selinuxenabled 2>/dev/null; then
    setfiles -F -r "$dest" /etc/selinux/targeted/contexts/files/file_contexts "$dest/usr"
fi
# workaround for https://github.com/systemd/systemd/issues/34387, as in
# toolbox-sysext-install.sh: restart the service instead of `refresh`
systemctl restart systemd-sysext.service
schemadir=usr/share/glib-2.0/schemas
if [ -d "$dest/$schemadir" ]; then
    glib-compile-schemas --targetdir "$dest/$schemadir" "/$schemadir"
    chown root:root "$dest/$schemadir/gschemas.compiled"
    if command -v setfiles >/dev/null && selinuxenabled 2>/dev/null; then
        setfiles -F -r "$dest" /etc/selinux/targeted/contexts/files/file_contexts "$dest/$schemadir"
    fi
    systemctl restart systemd-sysext.service
fi
systemd-sysext status --no-pager || true
STEPS
)
printf '%s\n' "$GUEST_STEPS" | vm_ssh sudo bash -s -- "$DEST" 2>&1 | sed 's/^/  guest: /' >&2 \
    || die "guest-side sysext steps failed"

(( RESTART )) && restart_session

# Report unresolved libraries of the dev build (a toolbox/guest package skew
# is the most common reason the shell then fails to start).
if vm_ssh "test -x /usr/bin/gnome-shell"; then
    missing=$(vm_ssh 'for f in /usr/bin/gnome-shell /usr/lib64/libmutter-*.so.*; do ldd "$f" 2>/dev/null; done | sort -u' | awk '/not found/' || true)
    if [[ -n $missing ]]; then
        log "WARNING: unresolved libraries in the guest (toolbox/guest package skew?):"
        printf '%s\n' "$missing" >&2
    fi
fi
log "sysext $NAME installed"
