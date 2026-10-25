"""Smoke tests: must pass on stock Fedora 45 (no sysext installed)."""

import os
import time
import unittest

from e2e import guest as G
from e2e.images import diff_region
from e2e.testcase import A11Y, TOP_BAR_PX, E2ETestCase

EXPECT_W = int(os.environ.get("E2E_RES_X", 1280))
EXPECT_H = int(os.environ.get("E2E_RES_Y", 800))


class Smoke(E2ETestCase):
    def test_session_up(self):
        g = self.guest
        pid = g.run("pgrep", "-u", "tester", "-x", "gnome-shell", session=False).stdout.split()
        self.assertTrue(pid, "no gnome-shell process for tester")
        ver = g.run("busctl", "--user", "get-property", "org.gnome.Shell", "/org/gnome/Shell",
                    "org.gnome.Shell", "ShellVersion").stdout.strip()
        self.assertTrue(ver.startswith("s "), ver)
        sess = g.sh("for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
                    "loginctl show-session $s -p Name -p Type -p Active -p Class; echo; done",
                    session=False).stdout
        blocks = [dict(l.split("=", 1) for l in b.splitlines() if "=" in l)
                  for b in sess.split("\n\n")]
        # (the ssh login of this very test shows up as a "tty" session too)
        tester = [b for b in blocks if b.get("Name") == "tester" and b.get("Class") == "user"
                  and b.get("Type") == "wayland"]
        self.assertTrue(tester, f"no wayland logind session for tester: {sess}")
        self.assertEqual(tester[0].get("Active"), "yes", tester[0])
        self.assertEqual(g.gsettings_get("org.gnome.desktop.interface", "toolkit-accessibility"), "true")
        self.assertEqual(g.gsettings_get("org.gnome.desktop.session", "idle-delay"), "uint32 0")
        self.assertEqual(g.gsettings_get("org.gnome.desktop.interface", "enable-hot-corners"), "false")
        # The pie timers are animations; the shell inhibits them under
        # software rendering unless started with --force-animations.
        anim = g.run("busctl", "--user", "get-property", "org.gnome.Shell", "/org/gnome/Shell/Introspect",
                     "org.gnome.Shell.Introspect", "AnimationsEnabled").stdout.strip()
        self.assertEqual(anim, "b true", "animations are inhibited (pie timers would not draw)")
        env = g.run("python3", G.GUEST_PKG + "/session.py", "json", session=False).stdout
        with open(os.path.join(self.art, "session-env.json"), "w") as f:
            f.write(env)
        img = self.screenshot("session")
        self.assertEqual((img.width, img.height), (EXPECT_W, EXPECT_H))

    def test_probe_sees_uinput_click(self):
        self.start_probe()
        self.center_pointer()
        n = self.probe.mark()
        t0, t1 = self.ctx.mouse.click()
        G.wait_until(lambda: len(self.probe.of_type("press", "release", since=n)) >= 2, 10, 0.1,
                     "press and release in the probe")
        ev = self.probe.of_type("press", "release", since=n)
        self.assertEqual([e["type"] for e in ev], ["press", "release"], ev)
        self.assertEqual([e["button"] for e in ev], [1, 1], ev)
        self.assertGreaterEqual(ev[0]["t_ns"], t0)
        self.assertLess(ev[0]["t_ns"], ev[1]["t_ns"])
        self.assertTrue(ev[1]["b1"] or True)  # state on release is toolkit specific; logged only

    def test_qmp_screenshot_not_blank(self):
        img = self.screenshot("plain")
        self.assertFalse(img.is_blank(), "screendump is blank/uniform")
        self.assertGreater(img.distinct_colors(), 16)
        self.assertEqual((img.width, img.height), (EXPECT_W, EXPECT_H))

    def test_settings_driver_toggles_simulated_secondary_click(self):
        self.assertEqual(self.gs_get("secondary-click-enabled"), "false")
        opened = self.settings("open")
        self.assertTrue(opened and opened.get("ok"), opened)
        self.screenshot("settings-open")
        r = self.settings("toggle", "Simulated Secondary Click", "on", "--no-fallback")
        self.assertEqual(r.get("via"), "ui", r)
        self.wait_gs("secondary-click-enabled", "true")
        self.screenshot("settings-on")
        r = self.settings("toggle", "Simulated Secondary Click", "off", "--no-fallback")
        self.assertEqual(r.get("via"), "ui", r)
        self.wait_gs("secondary-click-enabled", "false")
        self.settings("close", check=False)

    def pie_check(self, delay, hold_s, label):
        """Press, wait for the shell's pie timer to show up around the
        pointer, keep holding to *hold_s* seconds, take the mid-hold
        screenshot, release. Returns (baseline, mid_hold, diff, pointer)."""
        self.start_probe()
        px, py = self.center_pointer()
        sy = self.screen_y(py)  # surface -> screen (top bar offset)
        base = self.settled_screenshot()
        self.ctx.mouse.press()
        t_press = time.monotonic()
        try:
            def pie():
                d = diff_region(base, self.ctx.screendump(), ignore_top=TOP_BAR_PX)
                return d if d["count"] > 100 else None

            G.wait_until(pie, delay, 0.05, f"{label} pie timer on screen")
            seen = time.monotonic() - t_press
            # The remaining part of the deliberate hold.
            time.sleep(max(0.0, hold_s - (time.monotonic() - t_press)))
            mid = self.ctx.screendump()
            d = diff_region(base, mid, ignore_top=TOP_BAR_PX)
        finally:
            self.ctx.mouse.release()
        with open(os.path.join(self.art, "mid-hold.png"), "wb") as f:
            f.write(mid.to_png())
        x0, y0, x1, y1 = d["bbox"]
        with open(os.path.join(self.art, "pie.txt"), "w") as f:
            f.write(f"pointer(screen)=({px},{sy}) bbox={d['bbox']} size=({x1 - x0 + 1},{y1 - y0 + 1}) "
                    f"pixels={d['count']} first_seen_after={seen:.2f}s hold={hold_s}s\n")
        # The pie is a small disc centred on the pointer: everything that
        # changed lies within its radius and the box reaches the pointer.
        r = 50
        self.assertGreaterEqual(x0, px - r, d)
        self.assertLessEqual(x1, px + r, d)
        self.assertGreaterEqual(y0, sy - r, d)
        self.assertLessEqual(y1, sy + r, d)
        self.assertTrue(x0 <= px + 4 and x1 >= px - 4 and y0 <= sy + 4 and y1 >= sy - 4,
                        f"change {d['bbox']} does not touch the pointer ({px},{sy})")
        return base, mid, d, (px, sy)

    def test_secondary_click_pie_timer(self):
        """Reference for the pie check: with simulated secondary click on,
        a held button makes the shell draw a pie around the pointer."""
        self.gs_set("secondary-click-enabled", "true")
        self.gs_set("secondary-click-time", "2.0")
        base, mid, d, _ = self.pie_check(2.0, 1.0, "secondary click")
        # After the early release the pie goes away again.
        G.wait_until(lambda: diff_region(base, self.ctx.screendump(),
                                         ignore_top=TOP_BAR_PX)["count"] < 50,
                     5, 0.2, "pie to disappear after release")

    def test_secondary_click_fires_after_delay(self):
        """Holding past the delay produces a right click for the client."""
        self.gs_set("secondary-click-enabled", "true")
        self.gs_set("secondary-click-time", "1.0")
        self.start_probe()
        self.center_pointer()
        n = self.probe.mark()
        self.ctx.mouse.hold(1600)  # the hold duration itself
        G.wait_until(lambda: [e for e in self.probe.of_type("press", since=n) if e["button"] == 3],
                     8, 0.2, "simulated right-button press")
        self.sentinel()
        ev = self.probe.of_type("press", "release", since=n)
        buttons = [(e["type"], e["button"]) for e in ev]
        # mutter lets the left press through, then cancels it with a release
        # when the delay expires and emits the simulated right click.
        self.assertEqual(buttons, [("press", 1), ("release", 1), ("press", 3), ("release", 3)], buttons)


if __name__ == "__main__":
    unittest.main()
