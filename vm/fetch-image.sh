#!/usr/bin/env bash
# Download the Fedora 45 Cloud Base (Generic) qcow2 and verify it:
#   1. fetch the compose CHECKSUM file (clear-signed with the Fedora 45 key),
#   2. verify the signature with gpg in an isolated keyring that contains only
#      the pinned Fedora 45 primary key (from fedoraproject.org/fedora.gpg),
#   3. take the SHA256 line from the *signed text only* and compare it with
#      the sha256 of the downloaded image.
# Prints the image path on stdout and writes var/dl/IMAGE.txt.
#
# Source selection (first hit wins), override with E2E_IMAGE_BASE_URL +
# E2E_IMAGE_FILE + E2E_CHECKSUM_FILE:
#   releases/45/Cloud/x86_64/images          (GA)
#   releases/test/45_*/Cloud/x86_64/images   (Beta)
# The branched nightly compose (development/45) is NOT used: its CHECKSUM file
# is not signed.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Fedora (45) primary key, as published on https://fedoraproject.org/security
F45_FPR=4F50A6114CD5C6976A7F1179655A4B02F577861E
MIRROR=${E2E_MIRROR:-https://dl.fedoraproject.org/pub/fedora/linux}
DL=$E2E_VAR/dl
mkdir -p "$DL/keys"

list_dir() { curl -sSfL --max-time 60 "$1/" 2>/dev/null | rg -o 'href="[^"?/][^"]*"' | sed 's/^href="//; s/"$//; s:/$::'; }

if [[ -z ${E2E_IMAGE_BASE_URL:-} ]]; then
    base=
    if list_dir "$MIRROR/releases/45/Cloud/x86_64/images" | rg -q 'Fedora-Cloud-Base-Generic-45-.*qcow2$'; then
        base=$MIRROR/releases/45/Cloud/x86_64/images
    else
        beta=$(list_dir "$MIRROR/releases/test" | rg '^45_' | sort -V | tail -n1 || true)
        [[ -n $beta ]] || die "no Fedora 45 GA or Beta compose found under $MIRROR/releases"
        base=$MIRROR/releases/test/${beta%/}/Cloud/x86_64/images
    fi
    E2E_IMAGE_BASE_URL=$base
fi
if [[ -z ${E2E_IMAGE_FILE:-} ]]; then
    E2E_IMAGE_FILE=$(list_dir "$E2E_IMAGE_BASE_URL" | rg '^Fedora-Cloud-Base-Generic-45.*\.x86_64\.qcow2$' | sort -V | tail -n1)
    [[ -n $E2E_IMAGE_FILE ]] || die "no Generic qcow2 listed at $E2E_IMAGE_BASE_URL"
fi
if [[ -z ${E2E_CHECKSUM_FILE:-} ]]; then
    E2E_CHECKSUM_FILE=$(list_dir "$E2E_IMAGE_BASE_URL" | rg 'CHECKSUM$' | sort -V | tail -n1)
    [[ -n $E2E_CHECKSUM_FILE ]] || die "no CHECKSUM file listed at $E2E_IMAGE_BASE_URL"
fi
log "image:    $E2E_IMAGE_BASE_URL/$E2E_IMAGE_FILE"
log "checksum: $E2E_IMAGE_BASE_URL/$E2E_CHECKSUM_FILE"

# --- signing key -----------------------------------------------------------
if [[ ! -s $DL/keys/fedora.gpg ]]; then
    curl -sSfL --max-time 60 -o "$DL/keys/fedora.gpg" https://fedoraproject.org/fedora.gpg
fi
export GNUPGHOME=$DL/keys/gnupg
rm -rf "$GNUPGHOME"; mkdir -m 700 "$GNUPGHOME"
# Import only the pinned key, and only if its fingerprint is what we expect.
gpg --batch --quiet --import-options import-show --import "$DL/keys/fedora.gpg" >/dev/null 2>&1 || true
gpg --batch --list-keys --with-colons 2>/dev/null | rg -q "^fpr:::::::::${F45_FPR}:" \
    || die "pinned Fedora 45 key $F45_FPR is not in fedora.gpg"
# Best-effort cross-check against the page Fedora publishes fingerprints on.
if curl -sSfL --max-time 60 https://fedoraproject.org/security/ -o "$DL/keys/security.html" 2>/dev/null; then
    tr -d ' \n' <"$DL/keys/security.html" | rg -qi "$F45_FPR" \
        || log "WARNING: $F45_FPR not found on fedoraproject.org/security (page layout change?)"
fi

# --- files -----------------------------------------------------------------
curl -sSfL --max-time 120 -o "$DL/$E2E_CHECKSUM_FILE" "$E2E_IMAGE_BASE_URL/$E2E_CHECKSUM_FILE"
if [[ ! -s $DL/$E2E_IMAGE_FILE ]]; then
    log "downloading image (resumable)"
    curl -fsSL -C - --retry 5 --retry-delay 3 -o "$DL/$E2E_IMAGE_FILE" "$E2E_IMAGE_BASE_URL/$E2E_IMAGE_FILE" >&2
fi

# --- verify signature ------------------------------------------------------
status=$(gpg --batch --yes --status-fd 1 --output "$DL/$E2E_CHECKSUM_FILE.signed-text" --decrypt "$DL/$E2E_CHECKSUM_FILE" 2>/dev/null || true)
echo "$status" | rg -q '^\[GNUPG:\] GOODSIG ' || die "bad or missing signature on $E2E_CHECKSUM_FILE"
echo "$status" | rg -q "^\[GNUPG:\] VALIDSIG ${F45_FPR} " || die "CHECKSUM not signed by the pinned Fedora 45 key"
log "GPG: good signature by Fedora (45) primary key $F45_FPR"

# --- verify hash (from the signed text only) -------------------------------
want=$(rg -o "^SHA256 \(${E2E_IMAGE_FILE//./\\.}\) = ([0-9a-f]{64})\$" -r '$1' "$DL/$E2E_CHECKSUM_FILE.signed-text" | head -n1)
[[ -n $want ]] || die "no SHA256 line for $E2E_IMAGE_FILE in the signed checksum text"
have=$(sha256sum "$DL/$E2E_IMAGE_FILE" | cut -d' ' -f1)
if [[ $want != "$have" ]]; then
    die "SHA256 mismatch for $E2E_IMAGE_FILE: signed=$want actual=$have (delete $DL/$E2E_IMAGE_FILE and retry)"
fi
log "SHA256 OK: $have"

cat >"$DL/IMAGE.txt" <<TXT
image_url=$E2E_IMAGE_BASE_URL/$E2E_IMAGE_FILE
checksum_url=$E2E_IMAGE_BASE_URL/$E2E_CHECKSUM_FILE
signing_key=$F45_FPR
sha256=$have
verified=$(date -u +%FT%TZ)
TXT
echo "$DL/$E2E_IMAGE_FILE"
