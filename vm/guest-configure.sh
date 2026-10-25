#!/usr/bin/bash
# Runs as root INSIDE the guest (streamed over ssh by provision.sh).
# Part 2 of 2: configure an unattended, autologin, accessibility-enabled GNOME
# Wayland session for "tester". Idempotent; `provision.sh --reconfigure` re-runs
# only this on top of the existing base image.
set -euxo pipefail
U=tester
export LC_ALL=C.UTF-8

# --- boot target, login manager --------------------------------------------
systemctl set-default graphical.target
systemctl enable sshd.service gdm.service
cat >/etc/gdm/custom.conf <<'CONF'
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=tester
WaylandEnable=true
InitialSetupEnable=False

[security]

[xdmcp]

[chooser]

[debug]
CONF
echo 'LANG=en_US.UTF-8' >/etc/locale.conf

# --- no first-run wizards ---------------------------------------------------
install -d -o $U -g $U -m 0755 /home/$U/.config /home/$U/.config/autostart
printf 'yes\n' >/home/$U/.config/gnome-initial-setup-done
chown $U:$U /home/$U/.config/gnome-initial-setup-done
systemctl --global mask gnome-initial-setup-first-login.service gnome-initial-setup-copy-worker.service || true
# Per-user autostart entries with the same file name override system ones.
for f in org.gnome.Software.desktop gnome-software-service.desktop \
         org.freedesktop.problems.applet.desktop gnome-initial-setup-first-login.desktop \
         org.gnome.SettingsDaemon.Wacom.desktop; do
    printf '[Desktop Entry]\nType=Application\nName=disabled\nHidden=true\n' >/home/$U/.config/autostart/$f
done
chown -R $U:$U /home/$U/.config
systemctl mask packagekit.service dnf-makecache.timer dnf-makecache.service 2>/dev/null || true

# --- shell: behave like a hardware-accelerated session -----------------------
# gnome-shell inhibits all animations when the compositor renders in software
# (llvmpipe on virtio-gpu 2D). The pie timers (secondary click, click lock,
# dwell) are animations: without them they never appear. --force-animations
# is what the shell's own test tool passes.
install -d /etc/systemd/user/org.gnome.Shell@.service.d
cat >/etc/systemd/user/org.gnome.Shell@.service.d/10-e2e.conf <<'CONF'
[Service]
ExecStart=
ExecStart=/usr/bin/gnome-shell --mode=%i --force-animations
CONF

# --- system dconf defaults (no lock screen, no blanking, no suspend, a11y) -
install -d /etc/dconf/profile /etc/dconf/db/local.d
cat >/etc/dconf/profile/user <<'CONF'
user-db:user
system-db:local
CONF
cat >/etc/dconf/db/local.d/00-e2e <<'CONF'
[org/gnome/desktop/interface]
toolkit-accessibility=true
enable-hot-corners=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
sleep-inactive-ac-timeout=0
sleep-inactive-battery-timeout=0
power-button-action='nothing'
idle-dim=false

[org/gnome/shell]
welcome-dialog-last-shown-version='999.0'

[org/gnome/desktop/notifications]
show-banners=false

[org/gnome/desktop/privacy]
report-technical-problems=false
disable-microphone=false

[org/gnome/software]
download-updates=false
allow-updates=false
first-run=false

[org/gnome/desktop/peripherals/mouse]
accel-profile='flat'
speed=0.0
CONF
# Lock what must not drift (gsd-a11y-settings rewrites toolkit-accessibility
# to match the AT-SPI bus state at login, which otherwise leaves it false).
install -d /etc/dconf/db/local.d/locks
cat >/etc/dconf/db/local.d/locks/00-e2e <<'CONF'
/org/gnome/desktop/interface/toolkit-accessibility
/org/gnome/desktop/session/idle-delay
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
CONF
dconf update

# --- cloud-init is only for the first boot ----------------------------------
# Later boots (every overlay) must not search for a datasource.
touch /etc/cloud/cloud-init.disabled
install -d /var/lib/extensions
restorecon -R /home/$U /etc /var/lib/extensions 2>/dev/null || true

# --- SELinux policy store ---------------------------------------------------
# After the pcre2 upgrade the cached file_contexts.bin no longer loads
# ("Regex version mismatch" noise from setfiles/matchpathcon); rebuild it.
semodule -B || true

# --- GSettings schemas ------------------------------------------------------
# Seen on the Fedora 45 Beta + updates mix: after the big dnf transaction
# gschemas.compiled was stale (46 of 125 schemas, no org.gnome.SessionManager),
# so gnome-session aborted ("schema is not installed") and GDM gave up.
# desktop-backgrounds-gnome overrides org.gnome.desktop.screensaver
# picture-uri-dark, which gsettings-desktop-schemas no longer has, so a
# --strict compile exits with an error; a plain compile ignores the override.
glib-compile-schemas /usr/share/glib-2.0/schemas 2>&1 | { while read -r l; do case "$l" in *deprecated*) ;; *) echo "$l";; esac; done; } || true
gsettings list-schemas | { while read -r s; do [ "$s" = org.gnome.SessionManager ] && exit 0; done; exit 1; } \
    || { echo "org.gnome.SessionManager schema still missing" >&2; exit 1; }
chmod 0644 /usr/share/glib-2.0/schemas/gschemas.compiled
restorecon /usr/share/glib-2.0/schemas/gschemas.compiled 2>/dev/null || true

rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort >/var/tmp/e2e-packages.txt
cat /etc/os-release >/var/tmp/e2e-os-release.txt
sync
echo "guest provisioning finished"
