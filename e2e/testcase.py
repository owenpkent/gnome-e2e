"""Shared fixtures for the e2e tests: run context, artifacts, helpers."""

import json
import os
import re
import time
import unittest

from . import guest as G
from .images import Image, diff_region
from .qmp import QMP, QMPError
from .qmp_input import send_key

A11Y = "org.gnome.desktop.a11y.mouse"
TOP_BAR_PX = 40  # rows ignored when diffing screenshots (clock lives there)


class Context:
    """One per run: the guest, the QMP link, the shared uinput mouse."""

    def __init__(self, artifacts_root=None):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.root = artifacts_root or os.path.join(G.VAR, "artifacts", stamp)
        os.makedirs(self.root, exist_ok=True)
        self.run_dir = os.path.join(G.VAR, "run")
        self.qmp_path = os.path.join(self.run_dir, "qmp.sock")
        self.guest = G.Guest()
        self.probe = G.Probe(self.guest)
        self._qmp = None
        self._mouse = None
        self._shot = 0

    @property
    def qmp(self):
        if self._qmp is None:
            self._qmp = QMP(self.qmp_path, timeout=30).connect(retries=10)
        return self._qmp

    @property
    def mouse(self):
        if self._mouse is None or self._mouse.proc.poll() is not None:
            self._mouse = G.Mouse(self.guest)
        return self._mouse

    def has_mouse(self):
        return self._mouse is not None and self._mouse.proc.poll() is None

    def test_dir(self, test_id):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_id)
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def screendump(self):
        self._shot += 1
        path = os.path.join(self.run_dir, f"shot-{os.getpid()}-{self._shot}.ppm")
        try:
            self.qmp.screendump(path, "ppm")
        except (QMPError, OSError):
            self._qmp = None  # reconnect once
            self.qmp.screendump(path, "ppm")
        try:
            with open(path, "rb") as f:
                return Image.from_ppm(f.read())
        finally:
            os.unlink(path)

    def close(self):
        if self._mouse:
            self._mouse.close()
            self._mouse = None
        if self._qmp:
            self._qmp.close()
            self._qmp = None


_CTX = None


def set_context(ctx):
    global _CTX
    _CTX = ctx


def get_context():
    global _CTX
    if _CTX is None:
        _CTX = Context()
    return _CTX


class E2ETestCase(unittest.TestCase):
    """Base class: per-test artifacts dir, clean pointer/settings state."""

    # -- lifecycle ------------------------------------------------------
    def setUp(self):
        self.ctx = get_context()
        self.guest = self.ctx.guest
        self.probe = self.ctx.probe
        self.art = self.ctx.test_dir(self.id())
        self._shots = 0
        self.cursor = self.guest.journal_cursor()
        self._t0 = time.monotonic()
        self.cleanup_guest()

    def cleanup_guest(self):
        """Known state: no probe, no Settings, default a11y mouse keys."""
        g = self.guest
        if self.ctx.has_mouse():
            for b in ("left", "right", "middle"):
                self.ctx.mouse.release(b)
        g.run("pkill", "-f", "e2e_guest/probe.py", session=False, check=False)
        g.run("pkill", "-x", "gnome-control-c", session=False, check=False)
        g.run("pkill", "-f", "gnome-control-center", session=False, check=False)
        g.gsettings_reset_recursively(A11Y)

    def run(self, result=None):
        if result is None:
            return super().run(result)
        nbad = len(result.failures) + len(result.errors)
        nskip = len(result.skipped)
        out = super().run(result)
        skipped = len(result.skipped) != nskip
        failed = len(result.failures) + len(result.errors) != nbad
        if hasattr(self, "art") and not skipped:
            try:
                self._collect(result, failed)
            except Exception as exc:  # artifacts are best effort
                print(f"  (artifact collection failed: {exc})")
        return out

    def _collect(self, result, failed):
        art = self.art
        try:
            self.screenshot("final")
        except Exception as exc:
            with open(os.path.join(art, "final-screenshot-error.txt"), "w") as f:
                f.write(str(exc))
        text = self.guest.journal_since(self.cursor)
        with open(os.path.join(art, "journal.txt"), "w") as f:
            f.write(text)
        keep = re.compile(r"gnome-shell|gdm|mutter|gnome-control|Gjs|JS (ERROR|WARNING)|libinput", re.I)
        with open(os.path.join(art, "journal-shell-gdm.txt"), "w") as f:
            f.writelines(line + "\n" for line in text.splitlines() if keep.search(line))
        if self.probe.events:
            with open(os.path.join(art, "probe.jsonl"), "w") as f:
                f.write(self.probe.raw_text())
        details = []
        for kind, items in (("FAIL", result.failures), ("ERROR", result.errors)):
            for test, tb in items:
                if test is self:
                    details.append(f"{kind}\n{tb}")
        with open(os.path.join(art, "result.txt"), "w") as f:
            f.write(("FAILED\n" + "\n".join(details)) if failed else "passed\n")
            f.write(f"\nduration: {time.monotonic() - self._t0:.1f}s\n")
        self.guest.run("pkill", "-f", "e2e_guest/probe.py", session=False, check=False)
        self.guest.run("pkill", "-f", "gnome-control-center", session=False, check=False)

    # -- screenshots ----------------------------------------------------
    def screenshot(self, name=None):
        """QMP screendump -> Image; also saved as PNG in the test's dir."""
        img = self.ctx.screendump()
        self._shots += 1
        name = name or f"shot{self._shots:02d}"
        with open(os.path.join(self.art, f"{name}.png"), "wb") as f:
            f.write(img.to_png())
        return img

    def settled_screenshot(self, timeout=15.0):
        """Wait until two consecutive screenshots agree (animations done)."""
        prev = [self.ctx.screendump()]

        def stable():
            cur = self.ctx.screendump()
            same = diff_region(prev[0], cur, ignore_top=TOP_BAR_PX)["count"] == 0
            prev[0] = cur
            return cur if same else None

        img = G.wait_until(stable, timeout, 0.25, "a still screen")
        self._shots += 1
        with open(os.path.join(self.art, f"settled{self._shots:02d}.png"), "wb") as f:
            f.write(img.to_png())
        return img

    # -- probe + pointer ------------------------------------------------
    def start_probe(self, fullscreen=False):
        layout = self.probe.start(fullscreen=fullscreen)
        self.layout = (layout["width"], layout["height"])
        return layout

    def center_pointer(self, tol=60):
        """Put the pointer in the middle of the probe window, using the
        probe's own motion events as feedback (no assumption on accel)."""
        w, h = self.layout
        m = self.ctx.mouse
        m.move(-4000, -4000, steps=4)
        m.move(w // 2, h // 2 + 40, steps=max(1, (w // 2) // 10))
        for _ in range(8):
            pos = G.wait_until(self.probe.last_pointer, 15, 0.2, "pointer motion seen by probe")
            dx, dy = w / 2 - pos[0], h / 2 - pos[1]
            if abs(dx) <= tol and abs(dy) <= tol:
                return pos
            m.move(int(dx), int(dy))
        raise AssertionError(f"could not center the pointer, last seen at {self.probe.last_pointer()}")

    def sentinel(self):
        """Nudge the pointer and wait until the probe has seen that motion:
        every event emitted earlier is then already in the log (events are
        ordered), which makes 'nothing happened' assertions deterministic."""
        n = self.probe.mark()
        self.ctx.mouse.move(1, 0, steps=1)
        G.wait_until(lambda: self.probe.of_type("motion", since=n), 10, 0.1, "sentinel motion")
        self.ctx.mouse.move(-1, 0, steps=1)
        n2 = self.probe.mark()
        G.wait_until(lambda: self.probe.of_type("motion", since=n), 10, 0.1, "sentinel settle")
        return n2

    # -- Settings driver / gsettings -----------------------------------
    def settings(self, *args, check=True, timeout=90):
        """Run settings_driver.py in the guest; return its JSON output."""
        r = self.guest.run("python3", G.GUEST_PKG + "/settings_driver.py", *args,
                           check=False, timeout=timeout)
        out = r.stdout.strip().splitlines()
        data = None
        if out:
            try:
                data = json.loads(out[-1])
            except ValueError:
                data = None
        with open(os.path.join(self.art, "settings_driver.log"), "a") as f:
            f.write(f"$ settings_driver.py {' '.join(args)}\nrc={r.returncode}\n{r.stdout}\n{r.stderr}\n")
        if check and r.returncode != 0:
            raise AssertionError(f"settings_driver {args} failed rc={r.returncode}:\n{r.stdout}\n{r.stderr}")
        return data

    def gs_get(self, key):
        return self.guest.gsettings_get(A11Y, key)

    def gs_set(self, key, value):
        self.guest.gsettings_set(A11Y, key, value)

    def wait_gs(self, key, want, timeout=10):
        G.wait_until(lambda: self.gs_get(key) == want, timeout, 0.2, f"{A11Y} {key} == {want}")
