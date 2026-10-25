#!/usr/bin/env bash
# Exercise vm/install-sysext.sh with the dummy tree in tests/dummy-sysext
# (a payload file plus a new GSettings schema) against the running VM, then
# verify the merge, the schema compile and the session restart, and remove it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

"$E2E_ROOT/vm/install-sysext.sh" --from-dir "$E2E_ROOT/tests/dummy-sysext" --name e2e-dummy
vm_ssh 'test "$(cat /usr/share/gnome-e2e-dummy/hello.txt)" = "dummy sysext payload"' \
    || die "dummy payload is not visible under /usr"
out=$(vm_ssh 'python3 ~/e2e/e2e_guest/session.py run -- gsettings get org.gnome.e2e.dummy flag')
[[ $out == true ]] || die "new schema not usable (got: $out)"
log "dummy sysext merged, schema compiled, session restarted: OK"
"$E2E_ROOT/vm/install-sysext.sh" --name e2e-dummy --remove
vm_ssh 'test ! -e /usr/share/gnome-e2e-dummy' || die "dummy payload still merged after --remove"
log "dummy sysext removed: OK"
