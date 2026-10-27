"""ClickLock tests (mutter native latch + shell pie timer + Settings UI).

On a plain run (smoke suite, stock Fedora 45) these are skipped when the
guest's schema has no click-lock-enabled key. When the run is explicitly about
ClickLock (run-e2e.py --sysext, or --suite clicklock; ctx.require_clicklock) a
missing key or a missing Settings "Click Lock" row is a failure and the
Settings UI is the only way to enable the feature (--allow-skips restores the
old behaviour). Install the dev sysext with vm/install-sysext.sh to run them.

Latch behaviour under test (see gsettings-desktop-schemas): holding one of
the click-lock-buttons (primary by default, secondary and middle optional) for
click-lock-time keeps it logically pressed after the physical release, until
the next button press.
"""

import os
import time
import unittest

from e2e import guest as G
from e2e.images import diff_region
from e2e.testcase import A11Y, TOP_BAR_PX, E2ETestCase

ENABLED = "click-lock-enabled"
TIME = "click-lock-time"
BUTTONS = "click-lock-buttons"
# Settings check rows for click-lock-buttons, by the flag they set.
BUTTON_ROWS = {"secondary": "Secondary Button",
               "middle": "Middle Button"}
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
            msg = (f"{A11Y} has no {ENABLED} key in the guest (stock Fedora 45); "
                   "install the sysext with vm/install-sysext.sh")
            if c.require_clicklock:
                self.fail(msg + " (this run is about ClickLock, so this is a failure; "
                          "--allow-skips to skip instead)")
            raise unittest.SkipTest(msg)
        super().setUp()
        self.gs_set(TIME, str(DELAY))
        if c.guest.has_key(A11Y, BUTTONS):
            self.gs_set(BUTTONS, "['primary']")

    # -- helpers --------------------------------------------------------
    def ui_has_row(self, title):
        r = self.settings("has-row", title, check=False)
        return bool(r and r.get("found"))

    def enable_click_lock(self):
        """Via the Settings UI. Without the row: a failure when the run is
        about ClickLock, else (plain run, --allow-skips) gsettings."""
        strict = self.ctx.require_clicklock
        self.settings("open", check=strict)
        if self.ui_has_row(LOCK_ROW):
            r = self.settings("toggle", LOCK_ROW, "on", "--no-fallback")
            self.assertEqual(r.get("via"), "ui", r)
            self.via = "ui"
        elif strict:
            self.fail(f"Settings has no '{LOCK_ROW}' row: the UI path is mandatory in a "
                      "ClickLock run (--allow-skips to fall back to gsettings)")
        else:
            self.gs_set(ENABLED, "true")
            self.via = "gsettings"
        self.wait_gs(ENABLED, "true")
        self.settings("close", check=False)

    def lock_the_button(self, button="left"):
        """Hold longer than the delay, release; return probe mark before."""
        n = self.probe.mark()
        self.ctx.mouse.hold(HOLD_MS, button)
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
        """The pie appears during the hold, fills, and follows the pointer.

        Two holds, the pointer moved between them: the painted region must
        grow within each hold and shift by the pointer's own displacement.
        The probe reports window coordinates, so only the delta is compared.
        """
        p1 = self.prepare()
        self.gs_set(TIME, "3.0")
        base = self.settled_screenshot()
        c1 = self._pie_center_during_hold(base, "mid-hold.png")

        self.ctx.mouse.move(200, 100, steps=10)
        p2 = G.wait_until(
            lambda: (lambda q: q if q != p1 and abs(q[0] - p1[0]) > 150 else None)(
                self.probe.last_pointer()),
            5, 0.1, "pointer moved")
        base = self.settled_screenshot()
        c2 = self._pie_center_during_hold(base, "mid-hold-moved.png")

        dx, dy = c2[0] - c1[0], c2[1] - c1[1]
        self.assertLess(abs(dx - (p2[0] - p1[0])), 20, (c1, c2, p1, p2))
        self.assertLess(abs(dy - (p2[1] - p1[1])), 20, (c1, c2, p1, p2))

    def _pie_center_during_hold(self, base, png):
        """Hold, wait for a small pie and then a clearly larger one, release
        before the 3 s delay, and return the larger one's bbox center."""
        found = {}

        def painted(min_count):
            img = self.ctx.screendump()
            d = diff_region(base, img, ignore_top=TOP_BAR_PX)
            if d["count"] <= min_count:
                return False
            found["img"], found["d"] = img, d
            return True

        self.ctx.mouse.press()
        try:
            G.wait_until(lambda: painted(150), 2.0, 0.05, "pie timer for click lock")
            first = found["d"]["count"]
            G.wait_until(lambda: painted(max(2 * first, 900)), 2.0, 0.05,
                         "pie timer to fill")
        finally:
            self.ctx.mouse.release()
        with open(os.path.join(self.art, png), "wb") as f:
            f.write(found["img"].to_png())
        x0, y0, x1, y1 = found["d"]["bbox"]
        # 60 px pie, at most scaled 2x in its zoom-out
        self.assertLess(x1 - x0, 130, found["d"])
        self.assertLess(y1 - y0, 130, found["d"])
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    # -- (d) mutual exclusion in the Settings UI ---------------------------
    def test_d_settings_click_lock_and_secondary_click_exclude_each_other(self):
        self.settings("open")
        if not self.ui_has_row(LOCK_ROW):
            msg = "Settings has no 'Click Lock' row (stock gnome-control-center)"
            if self.ctx.require_clicklock:
                self.fail(msg + " (this run is about ClickLock, so this is a failure)")
            self.skipTest(msg)
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

    # -- (f) secondary and middle buttons, picked in Settings -------------
    def test_f_secondary_and_middle_buttons_lock(self):
        # GDK numbers the middle button 2 (BUTTON2_MASK) and the right one 3.
        cases = (("right", 3, "b3"), ("middle", 2, "b2"))
        if not self.ctx.guest.has_key(A11Y, BUTTONS):
            msg = f"{A11Y} has no {BUTTONS} key in the guest"
            if self.ctx.require_clicklock:
                self.fail(msg)
            self.skipTest(msg)
        self.settings("open")
        if not self.ui_has_row(LOCK_ROW):
            msg = f"Settings has no '{LOCK_ROW}' row"
            if self.ctx.require_clicklock:
                self.fail(msg)
            self.skipTest(msg)
        self.settings("toggle", LOCK_ROW, "on", "--no-fallback")
        self.wait_gs(ENABLED, "true")
        for flag, title in BUTTON_ROWS.items():
            r = self.settings("toggle", title, "on", "--no-fallback")
            self.assertEqual(r.get("via"), "ui", r)
        self.wait_gs(BUTTONS, "['primary', 'secondary', 'middle']")
        self.screenshot("buttons-on")
        self.settings("close", check=False)
        self.start_probe()
        self.center_pointer()

        for button, gdk_button, mask in cases:
            with self.subTest(button=button):
                n = self.lock_the_button(button)
                self.sentinel()
                ev = self.events(n, "press", "release")
                self.assertEqual([(e["type"], e["button"]) for e in ev], [("press", gdk_button)],
                                 f"{button}: expected the press without a release, got {ev}")
                m = self.probe.mark()
                self.ctx.mouse.move(120, 0, steps=12, delay_ms=20)
                G.wait_until(lambda: len(self.events(m, "motion")) >= 3, 10, 0.1, "drag motion events")
                self.sentinel()
                motions = self.events(m, "motion")
                self.assertTrue(all(e[mask] for e in motions),
                                f"{button}: motion without its mask while locked: "
                                f"{[e for e in motions if not e[mask]][:3]}")
                self.assertEqual(self.events(m, "release"), [])

                # The next click of the same button releases it and is swallowed.
                k = self.probe.mark()
                self.ctx.mouse.click(button)
                G.wait_until(lambda: self.events(k, "release"), 10, 0.1, f"{button} release")
                self.sentinel()
                rel = self.events(n, "press", "release")
                self.assertEqual([(e["type"], e["button"]) for e in rel],
                                 [("press", gdk_button), ("release", gdk_button)], rel)
                q = self.probe.mark()
                self.ctx.mouse.move(-120, 0, steps=12, delay_ms=20)
                self.sentinel()
                self.assertFalse(any(e[mask] for e in self.events(q, "motion")),
                                 f"{button} still held after the unlocking click")


if __name__ == "__main__":
    unittest.main()
