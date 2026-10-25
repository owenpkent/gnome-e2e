#!/usr/bin/bash
# Runs as root INSIDE the guest (streamed over ssh by provision.sh).
# Installs the Workstation environment and configures an unattended,
# autologin, accessibility-enabled GNOME Wayland session for "tester".
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

# --- system dconf defaults (no lock screen, no blanking, no suspend, a11y) -
install -d /etc/dconf/profile /etc/dconf/db/local.d
cat >/etc/dconf/profile/user <<'CONF'
user-db:user
system-db:local
CONF
cat >/etc/dconf/db/local.d/00-e2e <<'CONF'
[org/gnome/desktop/interface]
toolkit-accessibility=true

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
dconf update

# --- cloud-init is only for the first boot ----------------------------------
# Later boots (every overlay) must not search for a datasource.
touch /etc/cloud/cloud-init.disabled
install -d /var/lib/extensions
restorecon -R /home/$U /etc /var/lib/extensions 2>/dev/null || true

rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort >/var/tmp/e2e-packages.txt
cat /etc/os-release >/var/tmp/e2e-os-release.txt
sync
echo "guest provisioning finished"
