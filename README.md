# gnome-e2e

A local end-to-end UI test harness: a Fedora 45 Workstation VM, driven from the
host, for testing the ClickLock merge requests (mutter, gnome-shell,
gnome-control-center, gsettings-desktop-schemas).

Why a VM: click lock lives in mutter's native seat implementation and only arms
for real libinput devices. Headless and devkit gnome-shell have no libinput, so
only a real session can run the whole chain: Settings UI -> gsettings -> mutter
latch -> gnome-shell pie timer -> a client that sees a held button. The dev
builds come from the `gnome-shell-devel` toolbox as a systemd-sysext, and
sysexts must match the host's os-release ID/VERSION_ID, hence a Fedora 45 guest.

Nothing here pushes anywhere, uses host sudo, or touches the source clones.

## Requirements (host)

`toolbox` + `podman` with the `gnome-shell-devel` container (it provides
`qemu-system-x86_64`; the host has no qemu), `/dev/kvm` usable inside it,
`curl`, `gpg`, `ssh`/`scp`/`ssh-keygen`, `python3` (>= 3.10, stdlib only),
`ripgrep` (`rg`, used by the scripts).

`/dev/kvm` access is a seat ACL: it disappears when nobody is logged in at the
console (the greeter owns it). `vm/start.sh` checks and says so. `E2E_ACCEL=tcg`
exists but a GNOME session under TCG is not usable.

`qemu-img` is not in the toolbox and toolbox `sudo` needs a password, so
`vm/get-qemu-img.sh` (called on demand) downloads Fedora's `qemu-img` RPM with
`dnf5 download`, checks its package signature with `rpm -K`, and unpacks it into
`var/tools/root`. Nothing is installed in the toolbox. (`sudo dnf install
qemu-img` inside the toolbox works too; the scripts use it when the local copy is absent.)

## One-time provisioning

    vm/provision.sh            # ~15 min, mostly the Workstation dnf transaction

* Downloads `Fedora-Cloud-Base-Generic-45_Beta-1.3.x86_64.qcow2` (Fedora 45 was
  not GA when this was written: `releases/` stops at 44). The script prefers
  `releases/45` once it exists, else the newest `releases/test/45_*`. The
  branched nightly compose is deliberately not used: its CHECKSUM file is not
  signed.
* Verification (`vm/fetch-image.sh`): the CHECKSUM file is clear-signed; it is
  checked with gpg against Fedora's published keyring
  (`https://fedoraproject.org/fedora.gpg`) in an isolated GNUPGHOME, and the
  signing key must be the pinned Fedora (45) primary key
  `4F50 A611 4CD5 C697 6A7F 1179 655A 4B02 F577 861E` (also cross-checked
  against `https://fedoraproject.org/security`). The SHA256 is then taken from
  the verified text only and compared with the image. Nothing downloaded is
  executed.
* Boots the image with cloud-init NoCloud (smbios serial pointing at a
  throwaway `python3 -m http.server` on the host, so no ISO tools are needed),
  creates `tester` (passwordless sudo, a dedicated key in `var/ssh/`, never your
  own keys), then over ssh runs `vm/guest-packages.sh` (`dnf upgrade`,
  `@workstation-product-environment`, `python3-evdev python3-dogtail
  python3-pyatspi`, ...) and `vm/guest-configure.sh`:
  graphical.target, GDM autologin (Wayland), no gnome-initial-setup / tour, no
  blanking, lock screen or suspend, `toolkit-accessibility true`, hot corners
  off, flat pointer acceleration, notification banners off (all via a system
  dconf db, the important keys locked), and a systemd user drop-in that starts
  gnome-shell with `--force-animations` (see below).
* Shuts down, flattens the result into `var/base.qcow2` and makes it read-only.
  `vm/provision.sh --reconfigure` re-runs only `guest-configure.sh` on top of
  the existing base (30 s), `--force` rebuilds from scratch.

The package set is Fedora 45 as of provisioning (Beta image plus a full
`dnf upgrade`). A sysext built against an older or newer toolbox may not match
the guest's libraries: `install-sysext.sh` runs `ldd` on the installed shell
and mutter and warns about unresolved libraries; compare
`var/base-packages.txt` with the toolbox's `rpm -qa`.

## The run loop

    vm/start.sh                 # fresh qcow2 overlay on base, waits for GNOME Shell (~16 s)
    ./run-e2e.py                # all suites against the running VM
    vm/stop.sh                  # discard (var/run/overlay.qcow2 stays until the next start)

or in one go: `./run-e2e.py --fresh [--sysext [DIR]]` (stops any VM, starts a
fresh overlay, optionally installs the sysext, runs, stops). Useful options:
`--suite smoke|clicklock|all`, `-k SUBSTRING`, `-v`, `--keep-vm`, `--list`,
`--allow-skips`, `--artifacts DIR`.

VM: KVM, `-cpu host`, 4 vCPUs, 6 GB, virtio-vga at 1280x800 (`-display none`,
llvmpipe), user-net with `127.0.0.1:2222 -> :22`, a QMP socket at
`var/run/qmp.sock`, the serial console in `var/run/serial.log`, a
`virtio-keyboard`. Override with `E2E_SSH_PORT`, `E2E_CPUS`, `E2E_MEM`,
`E2E_RES_X/Y`, `E2E_BOOT_TIMEOUT`, `E2E_KEEP_ON_FAIL=1`, `E2E_TOOLBOX`.
Log in by hand: `ssh -i var/ssh/id_ed25519 -p 2222 tester@127.0.0.1`.

## Installing a sysext

Assemble it in the toolbox as usual (`meson-build.sh --sysext` in mutter,
gnome-shell, gnome-control-center, gsettings-desktop-schemas), then

    vm/install-sysext.sh        # or: ./run-e2e.py --fresh --sysext

It `podman cp`s `/var/lib/extensions/gnome-shell-devel` out of the toolbox like
`toolbox-sysext-install.sh`, adds `usr/lib/extension-release.d/extension-release.gnome-shell-devel`
(the toolbox's `/etc/os-release`; ID and VERSION_ID must equal the guest's),
copies the tree into the guest's `/var/lib/extensions/`, relabels it for
SELinux (`setfiles -r`), `systemctl restart systemd-sysext`, recompiles the
GSettings schemas into the extension, restarts the sysext, then restarts GDM
and waits for the autologin session. `--from-dir DIR --name NAME` installs any
tree (`tests/dummy-sysext` plus `vm/test-dummy-sysext.sh` exercise the whole
path with a payload file and a new schema); `--remove` takes one out;
`--no-restart` skips the GDM restart (enough for schema-only changes).

## Tests

`tests/test_smoke.py` passes on stock Fedora 45: session up (wayland, a11y,
animations enabled), the probe window sees a uinput click as press+release, a
QMP screenshot is non-blank, `settings_driver.py` toggles "Simulated Secondary
Click" through the real UI (checked via gsettings), the secondary-click pie
timer shows up around the pointer mid-hold (the reference for the click lock
pie), and a hold past the delay yields the simulated right click.

`tests/test_clicklock.py` is skipped on a plain run when `gsettings range
org.gnome.desktop.a11y.mouse click-lock-enabled` fails in the guest. A run that
is explicitly about ClickLock (`--sysext`, or `--suite clicklock`) never skips:
a missing key or a missing Settings "Click Lock" row is a failure, the Settings
UI is the only way the tests enable the feature (no gsettings fallback), and the
exit code is nonzero on any failure, error or skip (`--allow-skips` turns that
off; `summary.json` records `clicklock_mode`, `allow_skips`, `exit_code` and the
skips with their reasons). Selecting no ClickLock test in that mode (for example
`--sysext --suite smoke`) exits 2 before the VM is touched. The tests:
(a) lock, drag with BUTTON1_MASK, release on the next click, exactly one
release; (b) a short click is a normal click; (c) mid-hold screenshot shows the
pie; (d) Click Lock and Simulated Secondary Click exclude each other in
Settings; (e) disabling while locked releases the button.

Waiting is condition based (`e2e.guest.wait_until`); the only sleeps are the
deliberate button-hold durations. "Nothing happened" assertions use a sentinel:
nudge the pointer and wait for the probe to see that motion; events are ordered,
so everything earlier is already in the log.

## Artifacts

`var/artifacts/<timestamp>/` with `summary.json`, `guest-info.txt` (kernel,
package versions, sysext status) and one directory per test: `final.png` and
other QMP screenshots (PNG written by `e2e/images.py`), `journal.txt` (guest
journal slice for that test), `journal-shell-gdm.txt` (gnome-shell / gdm / mutter
/ libinput / JS lines), `probe.jsonl`, `settings_driver.log`, `result.txt`
(with the traceback on failure), `pie.txt`.

## Guest side (`guest/e2e_guest/`, copied to `~tester/e2e/` by scp each run)

* `session.py`: derives the session environment for ssh commands from
  `/run/user/<uid>`: `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS=unix:path=<rt>/bus`,
  `WAYLAND_DISPLAY` (the `wayland-N` socket), plus `XDG_CURRENT_DESKTOP=GNOME`
  (gnome-control-center refuses to start without it), `XDG_SESSION_TYPE`,
  `GDK_BACKEND=wayland`, `LANG=en_US.UTF-8`. `session.py run -- CMD...` execs with it.
* `uinput_mouse.py`: one persistent root process, one uinput relative mouse
  (device named `e2e-uinput-mouse`, created once, never per command: the click
  lock drops its latch when the device disappears). Line protocol on stdin
  (`move DX DY [STEPS [MS]]`, `press|release|click [left|right|middle]`,
  `hold MS [btn]`, `sleep MS`, `quit`), reply `ok <t0_ns> <t1_ns>` on the guest's
  CLOCK_MONOTONIC. The host keeps one ssh channel open to it for the whole run.
* `probe.py`: maximized GTK4 window (`--fullscreen` available) that appends JSON
  lines for press, release, motion, enter, leave, scroll with `t_ns`
  (CLOCK_MONOTONIC), `x`/`y` (surface coordinates), `state`, `b1`. Note the GDK
  semantics: `state` is the state before the event, so `b1` is true on a
  release and on motion while held, false on the press. The window never redraws,
  so screenshots only change because of what the shell draws.
* `settings_driver.py`: AT-SPI (gi `Atspi`) driver for gnome-control-center, see
  `--help`: `open`, `has-row`, `toggle TITLE on|off|flip`, `get-switch`,
  `get-scale ROW SCALE`, `set-scale ROW SCALE VALUE`, `dump`, `close`, with a
  gsettings fallback (`--no-fallback` to forbid it).

## Things worth knowing (all found the hard way)

* Software rendering disables shell animations, and the pie timers are
  animations, so they never appear. `guest-configure.sh` installs
  `/etc/systemd/user/org.gnome.Shell@.service.d/10-e2e.conf` starting the shell
  with `--force-animations`. `test_session_up` asserts `AnimationsEnabled`.
* A fresh session starts in the Activities overview; windows opened then do not
  get key focus. Everything calls `OverviewActive=false` on `org.gnome.Shell`
  first. The top-left hot corner is disabled for the same reason (slamming the
  pointer into it opened the overview).
* GTK4 list rows expose no AT-SPI action, so the Settings driver reaches the
  "Pointing and Clicking" sub-page by walking Tab (focus read back from AT-SPI)
  and pressing Enter via mutter's `org.gnome.Mutter.RemoteDesktop` D-Bus API
  (a short-lived virtual keyboard, no root). Pointer events never go through it.
* A fresh Fedora 45 Beta + updates left `gschemas.compiled` stale (gnome-session
  aborted: "schema org.gnome.SessionManager is not installed"):
  `desktop-backgrounds-gnome` overrides a key that gsettings-desktop-schemas no
  longer has, which a `--strict` compile rejects. `guest-configure.sh` recompiles.
* The probe's `ready` line only means GTK mapped the window. The shell needs a few
  hundred ms more (a second or more on a busy guest) before the window can get the
  pointer; motion sent earlier reaches nobody, and mutter sends no enter to a
  window that appears under a pointer which then stands still, so the probe stays
  silent for good ("timed out waiting for pointer motion seen by probe", about 1
  run in 20). `center_pointer()` therefore wobbles the pointer by 2 px until the
  probe has seen an event (`pointer_seen()`), then corrects from fresh events.
* `pkill -f` over ssh matches its own shell; the code uses `-x` or `[x]pattern`.

## Known limits

* Pointer motion reaches the client as one event per frame; the drag test
  spreads 200 px over 0.4 s to get several.
* Scales are only reachable through the UI when their expander row is open
  (switching the row on opens it).
* Only the first monitor, one resolution, no HiDPI, no touch/tablet devices
  (ClickLock excludes tablets).
* The AT-SPI names are English and follow gnome-control-center 51: the page is
  the "Accessibility" panel, the nav row "Pointing and Clicking", the page title
  "Pointing & Clicking". A different Settings version may rename rows.
* TCG (no KVM) is untested.
