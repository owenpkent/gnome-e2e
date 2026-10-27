#!/usr/bin/env python3
"""Record a ClickLock demo video from the running VM.

Needs a VM with the ClickLock sysext (vm/start.sh, vm/install-sysext.sh, or
a run-e2e.py --keep-vm). Captures the screen with QMP screendumps into
ffmpeg, while it turns Click Lock on and ticks the button checkboxes in
Settings, then shows a normal drag and a locked drag for each button in a
demo window (guest/e2e_guest/demo_canvas.py) that draws a trail per button
the client sees held, with captions saying what the physical mouse does.

  demo/clicklock_demo.py OUT.mp4 [--fps 12] [--restart-session]

--restart-session first restarts the guest session with mutter's hardware
cursor disabled, so the pointer shows up in screendumps.
"""

import argparse
import os
import shlex
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from e2e import guest as G  # noqa: E402
from e2e.qmp import QMP  # noqa: E402

A11Y = "org.gnome.desktop.a11y.mouse"
MOUSE = "org.gnome.desktop.peripherals.mouse"
RUN = os.path.join(ROOT, "var", "run")
CAPTION = G.GUEST_DIR + "/caption.txt"
POINTER = "/tmp/demo-pointer.txt"
W, H = 1280, 800
LEFT = 120                      # trail start x, demo window coordinates
ROWS = (230, 340, 450, 560)     # trail rows: normal drag, primary, secondary, middle


class Recorder(threading.Thread):
    """Screendumps at a fixed rate, piped to ffmpeg as raw RGB."""

    def __init__(self, out, fps):
        super().__init__(daemon=True)
        self.fps, self.stop_flag, self.frames = fps, threading.Event(), 0
        self.qmp = QMP(os.path.join(RUN, "qmp.sock"))
        self.qmp.connect(retries=5)
        self.ffmpeg = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "veryfast",
             "-movflags", "+faststart", out], stdin=subprocess.PIPE)

    def run(self):
        path = os.path.join(RUN, "demo-frame.ppm")
        period = 1.0 / self.fps
        next_t = time.monotonic()
        frame = None
        while not self.stop_flag.is_set():
            try:
                self.qmp.screendump(path, "ppm")
                with open(path, "rb") as f:
                    data = f.read()
                frame = data[-W * H * 3:]
            except Exception as exc:  # keep the timeline: repeat the last frame
                print(f"screendump failed: {exc}", file=sys.stderr)
            # Write as many frames as wall time demands, so the video plays
            # in real time even when a screendump is slow.
            now = time.monotonic()
            while frame and next_t <= now:
                self.ffmpeg.stdin.write(frame)
                self.frames += 1
                next_t += period
            time.sleep(max(0.0, next_t - time.monotonic()))

    def finish(self):
        self.stop_flag.set()
        self.join()
        self.ffmpeg.stdin.close()
        self.ffmpeg.wait()
        self.qmp.close()


class Demo:
    def __init__(self, g):
        self.g = g
        self.mouse = None
        self.gain = 1.0  # window pixels per uinput unit

    def pointer(self):
        r = self.g.run("cat", POINTER, session=False, check=False)
        try:
            x, y = map(float, r.stdout.split())
            return x, y
        except ValueError:
            return None

    def wait_pointer(self, before):
        return G.wait_until(lambda: (p := self.pointer()) and p != before and p, 5, 0.15,
                            "pointer position from the demo window")

    def calibrate(self):
        """Pointer acceleration is not 1:1 in the guest; measure the gain."""
        self.mouse.move(2, 0, steps=1)
        a = self.wait_pointer(None)
        self.mouse.move(100, 0, steps=10, delay_ms=20)
        b = self.wait_pointer(a)
        self.gain = max(0.2, (b[0] - a[0]) / 100.0)
        print(f"pointer gain {self.gain:.2f}")

    def goto(self, x, y, ms=800):
        """Steer the pointer to window coordinates (x, y)."""
        for _ in range(6):
            p = self.pointer()
            if p and abs(p[0] - x) < 12 and abs(p[1] - y) < 12:
                return
            dx, dy = (x - p[0], y - p[1]) if p else (0, 0)
            self.move(int(dx / self.gain), int(dy / self.gain), ms)
            ms = 300
            time.sleep(0.3)

    def caption(self, text, pause=0.0):
        self.g.run("bash", "-c", f"printf %s {shlex.quote(text)} > {CAPTION}", session=False)
        time.sleep(pause)

    def settings(self, *args):
        r = self.g.run("python3", G.GUEST_PKG + "/settings_driver.py", *args, check=False, timeout=90)
        if r.returncode:
            raise RuntimeError(f"settings_driver {args}: {r.stdout} {r.stderr}")

    def move(self, dx, dy, ms=600):
        steps = max(1, int(ms / 20))
        self.mouse.move(dx, dy, steps=steps, delay_ms=ms / steps)

    def wave(self, dx, amp=45, n=4, ms=1600):
        """Move right by dx window pixels along a zigzag."""
        seg = dx / (2 * n) / self.gain
        a = amp / self.gain
        for _ in range(n):
            self.move(int(seg), int(-a), ms / (2 * n))
            self.move(int(seg), int(a), ms / (2 * n))

    def locked_drag(self, button, label, colour, row_y):
        self.goto(LEFT, row_y)
        self.caption(f"{label} button: hold it until the circle fills (1.2 s), then let go", 1.5)
        self.mouse.hold(1800, button)
        self.caption(f"Physically released. The app still sees {label} HELD, so moving drags ({colour}):",
                     0.6)
        self.wave(420)
        self.caption("One more click of the button releases the lock", 1.2)
        self.mouse.click(button)
        time.sleep(0.5)
        self.caption("Released: moving no longer draws", 0.3)
        self.move(int(-200 / self.gain), int(60 / self.gain), 900)
        time.sleep(0.8)

    def run(self):
        g = self.g
        g.gsettings_set(A11Y, "click-lock-enabled", "false")
        g.gsettings_set(A11Y, "secondary-click-enabled", "false")
        g.gsettings_set(A11Y, "click-lock-buttons", "['primary']")
        g.gsettings_set(A11Y, "click-lock-time", "1.2")
        g.gsettings_set(MOUSE, "accel-profile", "'flat'")
        g.run("pkill", "-f", "[d]emo_canvas.py", session=False, check=False)
        g.run("python3", G.GUEST_PKG + "/settings_driver.py", "close", check=False)
        self.caption("")

        # Settings: turn Click Lock on and pick the buttons.
        self.settings("open")
        time.sleep(2.0)
        self.settings("toggle", "Click Lock", "on", "--no-fallback")
        time.sleep(2.0)
        self.settings("toggle", "Middle Button", "on", "--no-fallback")
        time.sleep(1.5)
        self.settings("toggle", "Secondary Button", "on", "--no-fallback")
        time.sleep(1.5)
        # Scroll down to Delay and Motion Threshold.
        self.settings("focus-scale", "Click Lock", "Motion Threshold")
        time.sleep(3.5)
        self.settings("close")

        g.run("rm", "-f", POINTER, session=False)
        g.run("bash", "-c", f"nohup python3 {G.GUEST_PKG}/demo_canvas.py {CAPTION} {POINTER} "
              ">/tmp/demo-canvas.log 2>&1 </dev/null & disown", timeout=20)
        time.sleep(3.0)
        self.mouse = G.Mouse(g)
        time.sleep(2.0)  # until mutter has added the new uinput device
        # Bottom-left corner (top-left is the hot corner), then to the start.
        self.mouse.move(-3000, 3000, steps=60, delay_ms=5)
        self.move(100, -150, 500)
        self.calibrate()
        self.goto(LEFT, ROWS[0])

        self.caption("Without ClickLock a drag needs the button held down the whole time", 1.0)
        self.mouse.press("left")
        self.caption("Physically holding the primary button while moving:", 0.3)
        self.wave(420)
        self.mouse.release("left")
        self.caption("Released: the drag ends", 1.0)

        self.locked_drag("left", "Primary", "blue", ROWS[1])
        self.locked_drag("right", "Secondary", "red", ROWS[2])
        self.locked_drag("middle", "Middle", "green", ROWS[3])

        self.goto(700, ROWS[1])
        self.caption("Motion Threshold: moving more than 5 px before letting go cancels the lock", 1.5)
        self.mouse.press("left")
        self.caption("Physically holding the primary button and moving: an ordinary drag", 0.3)
        self.wave(300, ms=1800)
        time.sleep(0.4)
        self.mouse.release("left")
        self.caption("Released: nothing is locked, so moving does not draw", 0.3)
        self.move(int(-150 / self.gain), int(50 / self.gain), 900)
        time.sleep(0.8)
        self.goto(700, ROWS[0])

        self.caption("Short clicks stay ordinary clicks: nothing locks", 0.5)
        for b in ("left", "right", "middle"):
            self.mouse.click(b)
            time.sleep(0.4)
        self.wave(300, ms=1000)
        self.caption("Settings: untick Secondary Button ...", 0.3)
        self.settings("open")
        time.sleep(1.0)
        self.settings("expand", "Click Lock")
        time.sleep(0.8)
        self.settings("toggle", "Secondary Button", "off", "--no-fallback")
        time.sleep(1.5)
        self.settings("close")
        time.sleep(1.5)
        self.goto(700, ROWS[2])
        self.caption("... and a long secondary hold is an ordinary click again", 1.2)
        self.mouse.hold(1800, "right")
        self.caption("Released: nothing is held, so moving does not draw", 0.3)
        self.wave(300, ms=1000)
        self.caption("Done", 1.5)
        self.mouse.close()


def restart_session(g):
    env = "MUTTER_DEBUG_DISABLE_HW_CURSORS=1"
    g.run("bash", "-c", "mkdir -p ~/.config/environment.d && "
          f"echo {env} > ~/.config/environment.d/90-demo-cursor.conf", session=False)
    g.run("systemctl", "--user", "set-environment", env, session=False, check=False)
    g.run("systemctl", "restart", "gdm", session=False, sudo=True)
    time.sleep(5)
    G.wait_until(lambda: g.run("pgrep", "-u", "tester", "-x", "gnome-shell", session=False,
                               check=False).returncode == 0, 120, 1.0, "gnome-shell after restart")
    time.sleep(8)  # session settle: shell startup animation, autostart
    r = g.run("bash", "-c", "tr '\\0' '\\n' < /proc/$(pgrep -u tester -x gnome-shell)/environ "
              "| grep HW_CURSORS", session=False, check=False)
    print("gnome-shell env:", r.stdout.strip() or "(cursor variable missing)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--restart-session", action="store_true")
    a = ap.parse_args()
    g = G.Guest()
    g.push_package()
    if a.restart_session:
        restart_session(g)
    rec = Recorder(os.path.abspath(a.out), a.fps)
    rec.start()
    try:
        Demo(g).run()
    finally:
        rec.finish()
    print(f"{a.out}: {rec.frames} frames, {rec.frames / a.fps:.1f} s")


if __name__ == "__main__":
    main()
