#!/usr/bin/bash
# Runs as root INSIDE the guest (streamed over ssh by provision.sh).
# Part 1 of 2: package installation (slow). Part 2 is guest-configure.sh.
set -euxo pipefail
U=tester
export LC_ALL=C.UTF-8

# --- packages ---------------------------------------------------------------
dnf -y upgrade --refresh
dnf -y install @workstation-product-environment python3-evdev python3-dogtail python3-pyatspi \
    --setopt=install_weak_deps=True --skip-unavailable \
    || dnf -y group install workstation-product-environment
# Things the harness itself needs (python3-gobject/gtk4/at-spi2-core normally
# come with the environment; asking again is harmless).
dnf -y install --skip-unavailable python3-evdev python3-gobject gtk4 at-spi2-core \
    libinput-utils policycoreutils tar glibc-langpack-en
rpm -q python3-evdev python3-gobject gtk4 at-spi2-core gdm gnome-shell gnome-control-center mutter
