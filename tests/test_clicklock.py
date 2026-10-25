"""ClickLock tests (mutter native latch + shell pie timer + Settings UI).

Skipped when the guest's schema has no click-lock-enabled key (stock Fedora
45). Install the dev sysext with vm/install-sysext.sh to run them.

Latch behaviour under test (see gsettings-desktop-schemas): holding the
primary button for click-lock-time keeps it logically pressed after the
physical release, until the next button press.
"""

import os
import time
import unittest

from e2e import guest as G
from e2e.images import diff_region
from e2e.testcase import A11Y, TOP_BAR_PX, E2ETestCase

ENABLED = "click-lock-enabled"
TIME = "click-lock-time"
LOCK_ROW = "Click Lock"
SECONDARY_ROW = "Simulated Secondary Click"
DELAY = 1.0         # click-lock-time used by the tests, seconds
HOLD_MS = 1600      # physical hold used to arm the lock (> DELAY)


class ClickLock(E2ETestCase):
    _have_key = None

    def setUp(self):
        from e2e.testcase import get_context
        c = get_context()
        if ClickLock._have_key is None:
            ClickLock._have_key = c.guest.has_key(A11Y, ENABLED)
        if not ClickLock._have_key:
            raise unittest.SkipTest(
                f"{A11Y} has no {ENABLED} key in the guest (stock Fedora 45); "
                "install the sysext with vm/install-sysext.sh")
        super().setUp()
        self.gs_set(TIME, str(DELAY))

    # -- helpers --------------------------------------------------------
    def ui_has_row(self, title):
        r = self.settings("has-row", title, check=False)
        return bool(r and r.get("found"))

    def enable_click_lock(self):
        """Via the Settings UI when the row exists, else gsettings."""
        self.settings("open", check=False)
        if self.ui_has_row(LOCK_ROW):
            r = self.settings("toggle", LOCK_ROW, "on", "--no-fallback")
            self.assertEqual(r.get("via"), "ui", r)
            self.via = "ui"
        else:
            self.gs_set(ENABLED, "true")
            self.via = "gsettings"
        self.wait_gs(ENABLED, "true")
        self.settings("close", check=False)

    def lock_the_button(self):
        """Hold longer than the delay, release; return probe mark before."""
        n = self.probe.mark()
        self.ctx.mouse.hold(HOLD_MS)
        G.wait_until(lambda: self.probe.of_type("press", since=n), 5, 0.1, "press at the client")
        return n

    def events(self, n, *types):
        return [e for e in self.probe.of_type(*types, since=n)]

    def prepare(self):
        self.enable_click_lock()
        self.start_probe()
        return self.center_pointer()

    # -- (a) lock, drag, release -----------------------------------------
    def test_a_lock_holds_button_until_next_click(self):
        self.prepare()
        n = self.lock_the_button()
        self.sentinel()
        ev = self.events(n, "press", "release")
        self.assertEqual([(e["type"], e["button"]) for e in ev], [("press", 1)],
                         f"expected the press without a release, got {ev}")

        m = self.probe.mark()
        x_before, _ = self.probe.last_pointer()
        # 200 px over ~0.4 s so that the client gets several motion events
        # (GTK compresses motion to one event per frame).
        self.ctx.mouse.move(200, 0, steps=20, delay_ms=20)
        G.wait_until(lambda: len(self.events(m, "motion")) >= 3, 10, 0.1, "drag motion events")
        self.sentinel()
        motions = self.events(m, "motion")
        self.assertGreaterEqual(len(motions), 3)
        self.assertGreaterEqual(max(e["x"] for e in motions) - x_before, 150, motions)
        self.assertTrue(all(e["b1"] for e in motions),
                        f"motion without BUTTON1_MASK while locked: {[e for e in motions if not e['b1']][:3]}")
        self.assertEqual(self.events(m, "release"), [])

        k = self.probe.mark()
        self.ctx.mouse.click()
        G.wait_until(lambda: self.events(k, "release"), 10, 0.1, "release after the next click")
        self.sentinel()
        rel = self.events(n, "release")
        self.assertEqual(len(rel), 1, f"exactly one release expected, got {rel}")
        self.assertEqual(rel[0]["button"], 1)
        # and the button is really up again: plain motion has no button mask
        q = self.probe.mark()
        self.ctx.mouse.move(-100, 0)
        self.sentinel()
        self.assertFalse(any(e["b1"] for e in self.events(q, "motion")))

    # -- (b) short click is a normal click ------------------------------
    def test_b_short_click_is_normal(self):
        self.prepare()
        n = self.probe.mark()
        self.ctx.mouse.click()
        G.wait_until(lambda: self.events(n, "release"), 10, 0.1, "release of a short click")
        self.sentinel()
        ev = self.events(n, "press", "release")
        self.assertEqual([(e["type"], e["button"]) for e in ev], [("press", 1), ("release", 1)], ev)
        m = self.probe.mark()
        self.ctx.mouse.move(60, 0)
        self.sentinel()
        self.assertFalse(any(e["b1"] for e in self.events(m, "motion")), "latch armed by a short click")

    # -- (c) pie timer ----------------------------------------------------
    def test_c_mid_hold_screenshot_shows_pie(self):
        self.prepare()
        self.gs_set(TIME, "3.0")
        base = self.settled_screenshot()
        self.ctx.mouse.press()
        found = {}

        def pie():
            img = self.ctx.screendump()
            d = diff_region(base, img, ignore_top=TOP_BAR_PX)
            found["img"], found["d"] = img, d
            return d["count"] > 150

        try:
            G.wait_until(pie, 2.5, 0.05, "pie timer for click lock")
        finally:
            self.ctx.mouse.release()
        with open(os.path.join(self.art, "mid-hold.png"), "wb") as f:
            f.write(found["img"].to_png())
        x0, y0, x1, y1 = found["d"]["bbox"]
        self.assertLess(x1 - x0, 300)
        self.assertLess(y1 - y0, 300)

    # -- (d) mutual exclusion in the Settings UI ---------------------------
    def test_d_settings_click_lock_and_secondary_click_exclude_each_other(self):
        self.settings("open")
        if not self.ui_has_row(LOCK_ROW):
            self.skipTest("Settings has no 'Click Lock' row (stock gnome-control-center)")
        self.settings("toggle", SECONDARY_ROW, "on", "--no-fallback")
        self.wait_gs("secondary-click-enabled", "true")
        self.settings("toggle", LOCK_ROW, "on", "--no-fallback")
        self.wait_gs(ENABLED, "true")
        self.wait_gs("secondary-click-enabled", "false")
        self.screenshot("lock-on")
        self.settings("toggle", SECONDARY_ROW, "on", "--no-fallback")
        self.wait_gs("secondary-click-enabled", "true")
        self.wait_gs(ENABLED, "false")
        self.screenshot("secondary-on")
        self.settings("close", check=False)

    # -- (e) disabling while locked releases the button -------------------
    def test_e_disable_while_locked_releases(self):
        self.prepare()
        n = self.lock_the_button()
        self.sentinel()
        self.assertEqual(self.events(n, "release"), [])
        self.gs_set(ENABLED, "false")
        G.wait_until(lambda: self.events(n, "release"), 10, 0.1,
                     "release after disabling click lock while locked")
        self.sentinel()
        rel = self.events(n, "release")
        self.assertEqual(len(rel), 1, rel)
        q = self.probe.mark()
        self.ctx.mouse.move(60, 0)
        self.sentinel()
        self.assertFalse(any(e["b1"] for e in self.events(q, "motion")))


if __name__ == "__main__":
    unittest.main()
