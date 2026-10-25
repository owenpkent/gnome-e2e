#!/usr/bin/python3
"""Find the graphical session environment of the test user.

ssh logins have no session environment. Everything a GNOME client needs is
derivable from /run/user/<uid>:

  XDG_RUNTIME_DIR           /run/user/<uid>
  DBUS_SESSION_BUS_ADDRESS  unix:path=/run/user/<uid>/bus
  WAYLAND_DISPLAY           the wayland-N socket in the runtime dir

CLI:
  session.py env                 print `export K=V` lines (eval them in sh)
  session.py json                print the environment as JSON
  session.py run -- CMD ARGS...  exec CMD with the session environment
  session.py wait [SECONDS]      wait until gnome-shell answers on D-Bus
"""

import glob
import json
import os
import pwd
import subprocess
import sys
import time

USER = "tester"


def uid_of(user=USER):
    return pwd.getpwnam(user).pw_uid


def wayland_socket(runtime_dir):
    socks = []
    for path in sorted(glob.glob(os.path.join(runtime_dir, "wayland-*"))):
        if path.endswith(".lock"):
            continue
        socks.append(os.path.basename(path))
    return socks[0] if socks else None


def session_env(user=USER, base=None):
    """Return an environment dict for talking to the user's session."""
    uid = uid_of(user)
    rt = f"/run/user/{uid}"
    env = dict(os.environ if base is None else base)
    env["XDG_RUNTIME_DIR"] = rt
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={rt}/bus"
    wl = wayland_socket(rt)
    if wl:
        env["WAYLAND_DISPLAY"] = wl
    env["GDK_BACKEND"] = "wayland"
    # Settings pages are matched by English names.
    env.setdefault("LANG", "en_US.UTF-8")
    env["LC_ALL"] = env["LANG"]
    env.pop("XDG_DATA_DIRS", None)
    return env


def shell_version(env):
    r = subprocess.run(
        ["busctl", "--user", "get-property", "org.gnome.Shell", "/org/gnome/Shell",
         "org.gnome.Shell", "ShellVersion"],
        env=env, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def wait_for_shell(timeout=120.0, user=USER):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            env = session_env(user)
        except KeyError:
            env = None
        if env and os.path.exists(env["XDG_RUNTIME_DIR"] + "/bus") and "WAYLAND_DISPLAY" in env:
            if shell_version(env):
                return True
        time.sleep(1.0)
    return False


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "env"
    if cmd == "env":
        env = session_env()
        for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY",
                  "GDK_BACKEND", "LANG", "LC_ALL"):
            if k in env:
                print(f"export {k}='{env[k]}'")
        return 0
    if cmd == "json":
        env = session_env()
        keys = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY")
        print(json.dumps({k: env.get(k) for k in keys}))
        return 0
    if cmd == "run":
        args = argv[2:]
        if args and args[0] == "--":
            args = args[1:]
        if not args:
            print("session.py run -- CMD ...", file=sys.stderr)
            return 2
        os.execvpe(args[0], args, session_env())
    if cmd == "wait":
        timeout = float(argv[2]) if len(argv) > 2 else 120.0
        return 0 if wait_for_shell(timeout) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
