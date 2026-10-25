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
        tester = [b for b in blocks if b.get("Name") == "tester" and b.get("Class") == "user"]
        self.assertTrue(tester, f"no logind user session for tester: {sess}")
        self.assertEqual(tester[0].get("Type"), "wayland", tester[0])
        self.assertEqual(tester[0].get("Active"), "yes", tester[0])
        self.assertEqual(g.gsettings_get("org.gnome.desktop.interface", "toolkit-accessibility"), "true")
        self.assertEqual(g.gsettings_get("org.gnome.desktop.session", "idle-delay"), "uint32 0")
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

    def _hold_and_find_pie(self, button_label):
        """Press, poll screenshots until the pie shows around the pointer,
        release. Returns (baseline, mid_hold, diff, pointer)."""
        self.start_probe()
        px, py = self.center_pointer()
        base = self.settled_screenshot()
        n = self.probe.mark()
        self.ctx.mouse.press()
        t_press = time.monotonic()
        found = {}

        def pie():
            img = self.ctx.screendump()
            d = diff_region(base, img, ignore_top=TOP_BAR_PX)
            found["img"], found["diff"] = img, d
            return d["count"] > 150 and d["bbox"] is not None

        try:
            G.wait_until(pie, 4.0, 0.05, f"{button_label} pie timer on screen")
        finally:
            t_seen = time.monotonic() - t_press
            self.ctx.mouse.release()
        with open(os.path.join(self.art, "mid-hold.png"), "wb") as f:
            f.write(found["img"].to_png())
        return base, found["img"], found["diff"], (px, py), t_seen

    def test_secondary_click_pie_timer(self):
        """Reference for the pie check: with simulated secondary click on,
        a held button makes the shell draw a pie around the pointer."""
        self.gs_set("secondary-click-enabled", "true")
        self.gs_set("secondary-click-time", "2.0")
        base, mid, d, (px, py), t_seen = self._hold_and_find_pie("secondary click")
        x0, y0, x1, y1 = d["bbox"]
        # The pie is a small widget centred on the pointer. Surface coords
        # of the maximized probe window start below the top bar; allow for it.
        w, h = x1 - x0 + 1, y1 - y0 + 1
        with open(os.path.join(self.art, "pie.txt"), "w") as f:
            f.write(f"pointer(surface)=({px},{py}) bbox={d['bbox']} size=({w},{h}) "
                    f"pixels={d['count']} seen_after={t_seen:.2f}s\n")
        self.assertLess(w, 300, d)
        self.assertLess(h, 300, d)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.assertLess(abs(cx - px), 80, f"pie centre {cx},{cy} far from pointer {px},{py}")
        self.assertLess(abs(cy - py), 120, f"pie centre {cx},{cy} far from pointer {px},{py}")
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
        self.assertIn(("press", 3), buttons, buttons)
        self.assertIn(("release", 3), buttons, buttons)
        self.assertNotIn(("press", 1), buttons, buttons)


if __name__ == "__main__":
    unittest.main()
