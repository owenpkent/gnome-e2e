"""Host-side controller for the guest: ssh, the uinput mouse, the GTK probe."""

import json
import os
import selectors
import shlex
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("E2E_VAR", os.path.join(ROOT, "var"))
GUEST_DIR = "/home/tester/e2e"          # where guest/ is copied each run
GUEST_PKG = GUEST_DIR + "/e2e_guest"


class TimeoutError_(AssertionError):
    pass


def wait_until(fn, timeout=10.0, interval=0.1, desc="condition"):
    """Poll *fn* until it returns something truthy; return that value.

    Raises AssertionError on timeout. This is the harness's only waiting
    primitive besides the deliberate button-hold durations.
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError_(f"timed out after {timeout}s waiting for {desc}")
        time.sleep(interval)


class Guest:
    def __init__(self, port=None, user="tester", host="127.0.0.1", key=None):
        self.port = int(port or os.environ.get("E2E_SSH_PORT", 2222))
        self.user, self.host = user, host
        self.key = key or os.path.join(VAR, "ssh", "id_ed25519")

    # -- ssh plumbing ---------------------------------------------------
    def ssh_base(self):
        return ["ssh", "-F", "/dev/null", "-i", self.key, "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=6",
                "-p", str(self.port), f"{self.user}@{self.host}"]

    def run(self, *argv, session=True, sudo=False, timeout=60, check=True,
            input=None, binary=False):
        """Run argv in the guest. session=True runs it with the user's
        graphical-session environment (WAYLAND_DISPLAY etc.)."""
        cmd = list(argv)
        if session:
            cmd = ["python3", GUEST_PKG + "/session.py", "run", "--"] + cmd
        if sudo:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(self.ssh_base() + [shlex.join(cmd)], capture_output=True,
                           text=not binary, input=input, timeout=timeout)
        if check and r.returncode != 0:
            err = r.stderr if not binary else r.stderr.decode(errors="replace")
            raise RuntimeError(f"guest command failed ({r.returncode}): {shlex.join(cmd)}\n{err}")
        return r

    def sh(self, script, **kw):
        return self.run("bash", "-c", script, **kw)

    def up(self):
        try:
            return subprocess.run(self.ssh_base() + ["true"], capture_output=True,
                                  timeout=15).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    # -- files ----------------------------------------------------------
    def push_package(self):
        """scp the guest-side helper package into ~/e2e (fresh each run)."""
        self.run("rm", "-rf", GUEST_DIR, session=False)
        self.run("mkdir", "-p", GUEST_DIR, session=False)
        scp = ["scp", "-F", "/dev/null", "-i", self.key, "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR", "-P", str(self.port), "-r",
               os.path.join(ROOT, "guest", "e2e_guest"),
               f"{self.user}@{self.host}:{GUEST_DIR}/"]
        subprocess.run(scp, check=True, capture_output=True, timeout=60)
        self.run("find", GUEST_DIR, "-name", "__pycache__", "-prune", "-exec", "rm", "-rf", "{}", "+",
                 session=False, check=False)

    def read_file(self, path, offset=0):
        r = self.run("tail", "-c", f"+{offset + 1}", path, session=False, check=False, binary=True)
        return r.stdout if r.returncode == 0 else b""

    # -- gsettings ------------------------------------------------------
    def gsettings_get(self, schema, key):
        return self.run("gsettings", "get", schema, key).stdout.strip()

    def gsettings_set(self, schema, key, value):
        self.run("gsettings", "set", schema, key, str(value))

    def gsettings_reset_recursively(self, schema):
        self.run("gsettings", "reset-recursively", schema, check=False)

    def has_key(self, schema, key):
        return self.run("gsettings", "range", schema, key, check=False).returncode == 0

    # -- journal --------------------------------------------------------
    def journal_cursor(self):
        r = self.run("journalctl", "-b", "-n0", "--show-cursor", "--no-pager",
                     session=False, sudo=True, check=False)
        for line in r.stdout.splitlines():
            if line.startswith("-- cursor:"):
                return line.split(":", 1)[1].strip()
        return None

    def journal_since(self, cursor):
        args = ["journalctl", "-b", "--no-pager", "-o", "short-precise"]
        if cursor:
            args.append(f"--after-cursor={cursor}")
        return self.run(*args, session=False, sudo=True, check=False, timeout=120).stdout


class Mouse:
    """One persistent uinput mouse process (one device) for the whole run."""

    def __init__(self, guest):
        self.guest = guest
        argv = guest.ssh_base() + [shlex.join(
            ["sudo", "-n", "python3", "-u", GUEST_PKG + "/uinput_mouse.py"])]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.buf = b""
        line = self._readline(30)
        if not line.startswith("ready"):
            raise RuntimeError(f"uinput mouse failed to start: {line!r} {self._stderr()}")
        self.last_ns = None

    def _stderr(self):
        try:
            os.set_blocking(self.proc.stderr.fileno(), False)
            return (self.proc.stderr.read() or b"").decode(errors="replace")
        except Exception:
            return ""

    def _readline(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError_("uinput mouse did not answer in time")
            if self.sel.select(left):
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    raise RuntimeError(f"uinput mouse exited: {self._stderr()}")
                self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()

    def cmd(self, line, timeout=30):
        """Send one command; return (t_start_ns, t_end_ns) in guest
        CLOCK_MONOTONIC, the same clock the probe stamps events with."""
        self.proc.stdin.write(line.encode() + b"\n")
        self.proc.stdin.flush()
        reply = self._readline(timeout)
        if not reply.startswith("ok"):
            raise RuntimeError(f"uinput mouse: {line!r} -> {reply!r}")
        _, a, b = reply.split()
        return int(a), int(b)

    def move(self, dx, dy, steps=None, delay_ms=None):
        line = f"move {dx} {dy}"
        if steps is not None or delay_ms is not None:
            if steps is None:
                steps = max(1, -(-max(abs(dx), abs(dy)) // 10))
            line += f" {steps} {2 if delay_ms is None else delay_ms}"
        return self.cmd(line, timeout=30 + (steps or 1) * (delay_ms or 2) / 1000.0)

    def press(self, button="left"):
        return self.cmd(f"press {button}")

    def release(self, button="left"):
        return self.cmd(f"release {button}")

    def click(self, button="left"):
        return self.cmd(f"click {button}")

    def hold(self, ms, button="left"):
        return self.cmd(f"hold {ms} {button}", timeout=ms / 1000.0 + 30)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Probe:
    """The GTK4 pointer probe window in the guest."""

    def __init__(self, guest, path="/tmp/e2e-probe.jsonl"):
        self.guest, self.path = guest, path
        self._raw = b""
        self._parsed = 0
        self.events = []

    def start(self, fullscreen=False, timeout=60):
        g = self.guest
        g.run("pkill", "-f", "[e]2e_guest/probe.py", session=False, check=False)
        g.run("rm", "-f", self.path, session=False)
        self._raw, self._parsed, self.events = b"", 0, []
        opts = ["--fullscreen"] if fullscreen else []
        cmd = shlex.join(["python3", GUEST_PKG + "/probe.py", "--out", self.path] + opts)
        g.run("bash", "-c",
              f"nohup {cmd} >/tmp/e2e-probe.log 2>&1 </dev/null & disown", timeout=20)
        wait_until(lambda: self.find(type="ready"), timeout, 0.3, "probe window ready")
        return wait_until(lambda: self._maximized(), timeout, 0.3, "probe layout")

    def _maximized(self):
        lay = [e for e in self.poll() if e["type"] == "layout"]
        if lay and lay[-1]["width"] >= 600:
            return lay[-1]
        return None

    def stop(self):
        self.guest.run("pkill", "-f", "[e]2e_guest/probe.py", session=False, check=False)

    def poll(self):
        """Fetch new events from the guest file; return the full list."""
        data = self.guest.read_file(self.path, len(self._raw))
        if data:
            self._raw += data
            end = self._raw.rfind(b"\n") + 1
            fresh = self._raw[self._parsed:end]
            self._parsed = end
            for line in fresh.splitlines():
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass
        return self.events

    def find(self, **match):
        for ev in self.poll():
            if all(ev.get(k) == v for k, v in match.items()):
                return ev
        return None

    def of_type(self, *types, since=0):
        return [e for e in self.poll()[since:] if e["type"] in types]

    def mark(self):
        """Index to use as `since` for events that happen from now on."""
        return len(self.poll())

    def last_pointer(self):
        for ev in reversed(self.poll()):
            if "x" in ev and ev["type"] in ("motion", "press", "release", "enter"):
                return ev["x"], ev["y"]
        return None

    def raw_text(self):
        self.poll()
        return self._raw.decode(errors="replace")
